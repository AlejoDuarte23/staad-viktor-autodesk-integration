import ctypes
from typing import Any, Protocol

import comtypes.client
from comtypes import automation

class OpenSTAADGeometry(Protocol):
    def _FlagAsMethod(self, name: str) -> None: ...
    def GetMemberCount(self) -> int: ...
    def GetMemberIncidence(self, v_beam_no, v_node_a_byref, v_node_b_byref): ...
    def GetBeamList(self, beam_list_variant) -> None: ...
    def GetNodeCoordinates(self, v_node_no, v_x_byref, v_y_byref, v_z_byref) ->None: ...
    def IsBeam(self, v_member_no, v_tol_angle) -> automation.VARIANT: ...
    def IsColumn(self, v_member_no, v_tol_angle) -> automation.VARIANT: ...

class OpenSTAADProperty(Protocol):
    def GetBeamSectionDisplayName(self, beam_no) -> None:...


def get_openstaad_clients() -> tuple[OpenSTAADGeometry, OpenSTAADProperty]:
    """Return the geometry and property interfaces from OpenSTAAD."""

    os = comtypes.client.GetActiveObject("StaadPro.OpenSTAAD")
    geom: OpenSTAADGeometry = os.Geometry  # type: ignore[assignment]
    prop: OpenSTAADProperty = os.Property
    return geom, prop


def collect_geometry_data(units: str = "m") -> dict[str, Any]:
    """Collect connectivity data from the active STAAD model."""

    geometry, staad_property = get_openstaad_clients()
    beam_ids = get_beam_list(geometry)
    connectivity: dict[int, dict[str, float]] = {}
    lines: dict[int, dict[str, Any]] = {}

    # STAAD API returns coordinates in inches, convert to meters
    inches_to_meters = 0.0254

    for bid in beam_ids:
        na, nb = get_member_incidence(geometry=geometry, beam_no=bid)
        beam_name = get_beam_name(staad_property=staad_property, beamNo=bid)

        try:
            ax, ay, az = get_node_coords(geometry=geometry, node_no=na)
            bx, by, bz = get_node_coords(geometry=geometry, node_no=nb)
            # Convert from inches to meters
            ax, ay, az = ax * inches_to_meters, ay * inches_to_meters, az * inches_to_meters
            bx, by, bz = bx * inches_to_meters, by * inches_to_meters, bz * inches_to_meters
        except Exception as exc:  # pragma: no cover - defensive logging
            ax = ay = az = bx = by = bz = float("nan")
            print(f"Failed to get node coordinates for beam {bid}: {exc}")

        # Swap Y and Z: STAAD Y (vertical) -> Revit Z (vertical)
        if na not in connectivity:
            connectivity[na] = {"x": ax, "y": az, "z": ay}

        if nb not in connectivity:
            connectivity[nb] = {"x": bx, "y": bz, "z": by}

        # Convert section name:
        # - Replace uppercase X with lowercase x (e.g., UB457X152X52 -> UB457x152x52)
        # - Strip spaces (L 100x100x8 -> L100x100x8)
        section_name = beam_name.replace("X", "x").replace(" ", "") if beam_name else beam_name
        lines[bid] = {"nodeI": na, "nodeJ": nb, "section": section_name}

    return {"units": units, "connectivity": connectivity, "lines": lines}

def make_variant_i4(value: int) -> automation.VARIANT:
    """Create a VT_I4 VARIANT by value."""
    v = automation.VARIANT(value)
    v.vt = automation.VT_I4
    return v

def make_variant_r8(value: float) -> automation.VARIANT:
    """Create a VT_R8 VARIANT by value (float/double)."""
    v = automation.VARIANT(value)
    v.vt = automation.VT_R8
    return v

def make_variant_vt_ref(obj: object, var_type: int) -> automation.VARIANT:
    var = automation.VARIANT()
    var._.c_void_p = ctypes.addressof(obj)  # pass by reference
    var.vt = var_type | automation.VT_BYREF
    return var

def make_safe_array_long(size: int):
    return automation._midlSAFEARRAY(ctypes.c_long).create([0] * size)

def get_beam_list(geometry: OpenSTAADGeometry) -> list[int]:
    """
    Returns all member IDs in the current model.
    """

    geometry._FlagAsMethod("GetMemberCount")
    count = int(geometry.GetMemberCount())
    if count <= 0:
        return []

    sa_members = make_safe_array_long(count)
    v_members = make_variant_vt_ref(sa_members, automation.VT_ARRAY | automation.VT_I4)

    geometry._FlagAsMethod("GetBeamList")
    geometry.GetBeamList(v_members)

    raw = v_members.value
    data = raw if isinstance(raw, list) and (not raw or not isinstance(raw[0], list)) else raw[0]
    return [int(x) for x in data]

def get_member_incidence(geometry: OpenSTAADGeometry, beam_no: int) -> tuple[int, int]:
    """
    Returns the start and end node IDs for a member.
    """

    v_beam = make_variant_i4(int(beam_no))

    node_a = ctypes.c_long(0)
    node_b = ctypes.c_long(0)
    v_node_a = make_variant_vt_ref(node_a, automation.VT_I4)
    v_node_b = make_variant_vt_ref(node_b, automation.VT_I4)

    geometry._FlagAsMethod("GetMemberIncidence")
    ret = geometry.GetMemberIncidence(v_beam, v_node_a, v_node_b)

    ret_code = int(ret.value if isinstance(ret, automation.VARIANT) else ret)

    if ret_code < 0:
        if ret_code == -3001:
            raise RuntimeError(f"GetMemberIncidence failed with -3001, member {beam_no} not found")
        raise RuntimeError(f"GetMemberIncidence failed with code {ret_code}")

    return int(node_a.value), int(node_b.value)

def get_node_coords(geometry: OpenSTAADGeometry, node_no: int) -> tuple[float, float, float]:
    """
    Returns the (x, y, z) coordinates of the specified node in GLOBAL axes.
    """

    v_node = make_variant_i4(int(node_no))

    x = ctypes.c_double(0.0)
    y = ctypes.c_double(0.0)
    z = ctypes.c_double(0.0)
    v_x = make_variant_vt_ref(x, automation.VT_R8)
    v_y = make_variant_vt_ref(y, automation.VT_R8)
    v_z = make_variant_vt_ref(z, automation.VT_R8)

    geometry._FlagAsMethod("GetNodeCoordinates")
    geometry.GetNodeCoordinates(v_node, v_x, v_y, v_z)

    return float(x.value), float(y.value), float(z.value)

def is_beam(geometry: OpenSTAADGeometry, member_no: int, tol_angle_deg: float) -> int:
    """
    Returns 1 if the specified member is a BEAM within tolerance angle, 0 if not, or -3001 if member not found.
    """
    v_member = make_variant_i4(int(member_no))
    v_tol = make_variant_r8(float(tol_angle_deg))

    geometry._FlagAsMethod("IsBeam")
    ret = geometry.IsBeam(v_member, v_tol)
    return int(ret.value if isinstance(ret, automation.VARIANT) else ret)

def is_column(geometry: OpenSTAADGeometry, member_no: int, tol_angle_deg: float) -> int:
    """
    Returns 1 if the specified member is a COLUMN within tolerance angle, 0 if not, or -3001 if member not found.
    """
    v_member = make_variant_i4(int(member_no))
    v_tol = make_variant_r8(float(tol_angle_deg))

    geometry._FlagAsMethod("IsColumn")
    ret = geometry.IsColumn(v_member, v_tol)
    return int(ret.value if isinstance(ret, automation.VARIANT) else ret)

def get_beam_name(staad_property: OpenSTAADProperty, beamNo: int) -> str:
    """
    Returns the beam property name
    """
    staad_property._FlagAsMethod("GetBeamSectionDisplayName")
    return staad_property.GetBeamSectionDisplayName(beamNo)

if __name__ == "__main__":
    import json

    data = collect_geometry_data()
    with open("output.json", "w", encoding="utf-8") as jsonfile:
        json.dump(data, jsonfile, indent=4)

    print("Exported STAAD geometry to output.json")
