import logging
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Sequence, Type, Union

from aioca import (
    FORMAT_CTRL,
    FORMAT_RAW,
    FORMAT_TIME,
    CANothing,
    Subscription,
    caget,
    camonitor,
    caput,
)
from aioca.types import AugmentedValue, Dbr, Format
from bluesky.protocols import DataKey, Dtype, Reading
from epicscorelibs.ca import dbr

from ophyd_async.core import (
    ReadingValueCallback,
    SignalBackend,
    T,
    get_dtype,
    get_unique,
    wait_for_connection,
)
from ophyd_async.core.utils import DEFAULT_TIMEOUT, NotConnected

from .common import get_supported_enum_class

dbr_to_dtype: Dict[Dbr, Dtype] = {
    dbr.DBR_STRING: "string",
    dbr.DBR_SHORT: "integer",
    dbr.DBR_FLOAT: "number",
    dbr.DBR_CHAR: "string",
    dbr.DBR_LONG: "integer",
    dbr.DBR_DOUBLE: "number",
}


@dataclass
class CaConverter:
    read_dbr: Optional[Dbr]
    write_dbr: Optional[Dbr]

    def write_value(self, value) -> Any:
        return value

    def value(self, value: AugmentedValue):
        return value

    def reading(self, value: AugmentedValue):
        return {
            "value": self.value(value),
            "timestamp": value.timestamp,
            "alarm_severity": -1 if value.severity > 2 else value.severity,
        }

    def get_datakey(self, source: str, value: AugmentedValue) -> DataKey:
        return {"source": source, "dtype": dbr_to_dtype[value.datatype], "shape": []}


class CaLongStrConverter(CaConverter):
    def __init__(self):
        return super().__init__(dbr.DBR_CHAR_STR, dbr.DBR_CHAR_STR)

    def write_value(self, value: str):
        # Add a null in here as this is what the commandline caput does
        # TODO: this should be in the server so check if it can be pushed to asyn
        return value + "\0"


class CaArrayConverter(CaConverter):
    def get_datakey(self, source: str, value: AugmentedValue) -> DataKey:
        return {"source": source, "dtype": "array", "shape": [len(value)]}


@dataclass
class CaEnumConverter(CaConverter):
    enum_class: Type[Enum]

    def write_value(self, value: Union[Enum, str]):
        if isinstance(value, Enum):
            return value.value
        else:
            return value

    def value(self, value: AugmentedValue):
        return self.enum_class(value)

    def get_datakey(self, source: str, value: AugmentedValue) -> DataKey:
        choices = [e.value for e in self.enum_class]
        return {"source": source, "dtype": "string", "shape": [], "choices": choices}


class DisconnectedCaConverter(CaConverter):
    def __getattribute__(self, __name: str) -> Any:
        raise NotImplementedError("No PV has been set as connect() has not been called")


def make_converter(
    datatype: Optional[Type], values: Dict[str, AugmentedValue]
) -> CaConverter:
    pv = list(values)[0]
    pv_dbr = get_unique({k: v.datatype for k, v in values.items()}, "datatypes")
    is_array = bool([v for v in values.values() if v.element_count > 1])
    if is_array and datatype is str and pv_dbr == dbr.DBR_CHAR:
        # Override waveform of chars to be treated as string
        return CaLongStrConverter()
    elif is_array and pv_dbr == dbr.DBR_STRING:
        # Waveform of strings, check we wanted this
        if datatype and datatype != Sequence[str]:
            raise TypeError(f"{pv} has type [str] not {datatype.__name__}")
        return CaArrayConverter(pv_dbr, None)
    elif is_array:
        pv_dtype = get_unique({k: v.dtype for k, v in values.items()}, "dtypes")
        # This is an array
        if datatype:
            # Check we wanted an array of this type
            dtype = get_dtype(datatype)
            if not dtype:
                raise TypeError(f"{pv} has type [{pv_dtype}] not {datatype.__name__}")
            if dtype != pv_dtype:
                raise TypeError(f"{pv} has type [{pv_dtype}] not [{dtype}]")
        return CaArrayConverter(pv_dbr, None)
    elif pv_dbr == dbr.DBR_ENUM and datatype is bool:
        # Database can't do bools, so are often representated as enums, CA can do int
        pv_choices_len = get_unique(
            {k: len(v.enums) for k, v in values.items()}, "number of choices"
        )
        if pv_choices_len != 2:
            raise TypeError(f"{pv} has {pv_choices_len} choices, can't map to bool")
        return CaConverter(dbr.DBR_SHORT, dbr.DBR_SHORT)
    elif pv_dbr == dbr.DBR_ENUM:
        # This is an Enum
        pv_choices = get_unique(
            {k: tuple(v.enums) for k, v in values.items()}, "choices"
        )
        enum_class = get_supported_enum_class(pv, datatype, pv_choices)
        return CaEnumConverter(dbr.DBR_STRING, None, enum_class)
    else:
        value = list(values.values())[0]
        # Done the dbr check, so enough to check one of the values
        if datatype and not isinstance(value, datatype):
            raise TypeError(
                f"{pv} has type {type(value).__name__.replace('ca_', '')} "
                + f"not {datatype.__name__}"
            )
        return CaConverter(pv_dbr, None)


_tried_pyepics = False


def _use_pyepics_context_if_imported():
    global _tried_pyepics
    if not _tried_pyepics:
        ca = sys.modules.get("epics.ca", None)
        if ca:
            ca.use_initial_context()
        _tried_pyepics = True


class CaSignalBackend(SignalBackend[T]):
    def __init__(self, datatype: Optional[Type[T]], read_pv: str, write_pv: str):
        self.datatype = datatype
        self.read_pv = read_pv
        self.write_pv = write_pv
        self.initial_values: Dict[str, AugmentedValue] = {}
        self.converter: CaConverter = DisconnectedCaConverter(None, None)
        self.subscription: Optional[Subscription] = None

    def source(self, name: str):
        return f"ca://{self.read_pv}"

    async def _store_initial_value(self, pv, timeout: float = DEFAULT_TIMEOUT):
        try:
            self.initial_values[pv] = await caget(
                pv, format=FORMAT_CTRL, timeout=timeout
            )
        except CANothing as exc:
            logging.debug(f"signal ca://{pv} timed out")
            raise NotConnected(f"ca://{pv}") from exc

    async def connect(self, timeout: float = DEFAULT_TIMEOUT):
        _use_pyepics_context_if_imported()
        if self.read_pv != self.write_pv:
            # Different, need to connect both
            await wait_for_connection(
                read_pv=self._store_initial_value(self.read_pv, timeout=timeout),
                write_pv=self._store_initial_value(self.write_pv, timeout=timeout),
            )
        else:
            # The same, so only need to connect one
            await self._store_initial_value(self.read_pv, timeout=timeout)
        self.converter = make_converter(self.datatype, self.initial_values)

    async def put(self, value: Optional[T], wait=True, timeout=None):
        if value is None:
            write_value = self.initial_values[self.write_pv]
        else:
            write_value = self.converter.write_value(value)
        await caput(
            self.write_pv,
            write_value,
            datatype=self.converter.write_dbr,
            wait=wait,
            timeout=timeout,
        )

    async def _caget(self, format: Format) -> AugmentedValue:
        return await caget(
            self.read_pv,
            datatype=self.converter.read_dbr,
            format=format,
            timeout=None,
        )

    async def get_datakey(self, source: str) -> DataKey:
        value = await self._caget(FORMAT_CTRL)
        return self.converter.get_datakey(source, value)

    async def get_reading(self) -> Reading:
        value = await self._caget(FORMAT_TIME)
        return self.converter.reading(value)

    async def get_value(self) -> T:
        value = await self._caget(FORMAT_RAW)
        return self.converter.value(value)

    async def get_setpoint(self) -> T:
        value = await caget(
            self.write_pv,
            datatype=self.converter.read_dbr,
            format=FORMAT_RAW,
            timeout=None,
        )
        return self.converter.value(value)

    def set_callback(self, callback: Optional[ReadingValueCallback[T]]) -> None:
        if callback:
            assert (
                not self.subscription
            ), "Cannot set a callback when one is already set"
            self.subscription = camonitor(
                self.read_pv,
                lambda v: callback(self.converter.reading(v), self.converter.value(v)),
                datatype=self.converter.read_dbr,
                format=FORMAT_TIME,
            )
        else:
            if self.subscription:
                self.subscription.close()
            self.subscription = None
