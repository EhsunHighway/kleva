from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CParam:
    name:        str
    raw_type:    str
    base_type:   str
    is_pointer:  bool
    is_const:    bool
    is_array:    bool
    array_size:  int
    pointer_depth: int = 0

    def __post_init__(self) -> None:
        if self.pointer_depth == 0 and "*" in self.raw_type:
            self.pointer_depth = self.raw_type.count("*")


@dataclass
class CFunction:
    name:              str
    return_type:       str
    return_base:       str
    return_is_pointer: bool
    params:            list[CParam]


@dataclass(frozen=True)
class DerivedLocal:
    kind: str
    base: str
    arg:  str


@dataclass
class CFunctionPointerTypedef:
    name:        str
    return_type: str
    params:      list[CParam]


@dataclass
class CTypeCatalog:
    complete_structs: set[str] = field(default_factory=set)
    opaque_structs:   set[str] = field(default_factory=set)
    function_pointers: dict[str, CFunctionPointerTypedef] = field(default_factory=dict)
    struct_fields: dict[str, dict[str, CParam]] = field(default_factory=dict)

    def is_complete_struct(self, type_name: str) -> bool:
        return type_name in self.complete_structs

    def function_pointer(self, type_name: str) -> CFunctionPointerTypedef | None:
        return self.function_pointers.get(type_name)

    def field_type(self, type_name: str, field_name: str) -> CParam | None:
        return self.struct_fields.get(type_name, {}).get(field_name)
