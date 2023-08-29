import logging
from itertools import chain
from math import radians
from typing import Iterable, Iterator, Optional, Union, cast

from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace
from OCP.BRepFeat import BRepFeat
from OCP.BRepLib import BRepLib_FindSurface
from OCP.BRepTools import BRepTools
from OCP.GC import (
    GC_MakeArcOfCircle,
    GC_MakeArcOfEllipse,
    GC_MakeCircle,
    GC_MakeEllipse,
    GC_MakeSegment,
)
from OCP.GCPnts import GCPnts_QuasiUniformDeflection
from OCP.Geom import (
    Geom_BezierCurve,
    Geom_BSplineCurve,
    Geom_Circle,
    Geom_Curve,
    Geom_Ellipse,
    Geom_TrimmedCurve,
)
from OCP.GeomAbs import GeomAbs_CurveType, GeomAbs_Shape
from OCP.GeomAdaptor import GeomAdaptor_Curve
from OCP.GeomConvert import (
    GeomConvert,
    GeomConvert_ApproxCurve,
    GeomConvert_BSplineCurveToBezierCurve,
)
from OCP.gp import (
    gp_Ax1,
    gp_Ax2,
    gp_Circ,
    gp_Dir,
    gp_Elips,
    gp_Pnt,
    gp_Trsf,
    gp_Vec,
    gp_XYZ,
)
from OCP.ShapeExtend import ShapeExtend_WireData
from OCP.ShapeFix import ShapeFix_Wire
from OCP.Standard import Standard_Failure
from OCP.StdFail import StdFail_NotDone
from OCP.TColgp import TColgp_Array1OfPnt
from OCP.TopoDS import (
    TopoDS,
    TopoDS_Builder,
    TopoDS_Compound,
    TopoDS_Edge,
    TopoDS_Face,
    TopoDS_Iterator,
    TopoDS_Shape,
    TopoDS_Wire,
)

logger = logging.getLogger(__name__)


_TOLERANCE = 1e-8


#### types

VecLike = Union[
    gp_Pnt,
    gp_Vec,
    gp_XYZ,
    gp_Dir,
    tuple[float, float, float],
    tuple[float, float],
]
PntLike = VecLike
DirLike = VecLike


def as_Pnt(p: PntLike) -> gp_Pnt:
    return p if isinstance(p, gp_Pnt) else gp_Pnt(*as_triple(p))


def as_Vec(v: VecLike) -> gp_Vec:
    return v if isinstance(v, gp_Vec) else gp_Vec(*as_triple(v))


def as_Dir(d: DirLike) -> gp_Dir:
    return d if isinstance(d, gp_Dir) else gp_Dir(*as_triple(d))


def as_triple(p: VecLike) -> tuple[float, float, float]:
    if isinstance(p, (gp_Pnt, gp_Vec, gp_XYZ, gp_Dir)):
        return p.X(), p.Y(), p.Z()
    else:
        if len(p) == 3:
            return p[:3]
        elif len(p) == 2:
            return *p[:2], 0.0
        else:
            raise ValueError(f"cannot make point from {p!r}")


#### shapes


def make_compound(shapes: Iterable[TopoDS_Shape]):
    compound = TopoDS_Compound()
    comp_builder = TopoDS_Builder()
    comp_builder.MakeCompound(compound)

    for shape in shapes:
        comp_builder.Add(compound, shape)

    return compound


def bounding_box(
    shape_or_shapes: Union[TopoDS_Shape, Iterable[TopoDS_Shape]]
) -> Bnd_Box:
    bbox = Bnd_Box()
    for shape in (
        [shape_or_shapes]
        if isinstance(shape_or_shapes, TopoDS_Shape)
        else shape_or_shapes
    ):
        BRepBndLib.AddOptimal_s(shape, bbox)
    return bbox


def topoDS_iterator(
    shape: TopoDS_Shape, with_orientation: bool = True, with_location: bool = True
):
    iterator = TopoDS_Iterator(shape, with_orientation, with_location)
    while iterator.More():
        yield iterator.Value()
        iterator.Next()


#### faces


def face_outer_wire(face: TopoDS_Face):
    """Find the outer wire of a face."""
    return BRepTools.OuterWire_s(face)


def face_inner_wires(face: TopoDS_Face):
    """Find the inner wires of a face."""
    outer = face_outer_wire(face)
    return [cast(TopoDS_Wire, w) for w in topoDS_iterator(face) if not w.IsSame(outer)]


def face_from_wires(
    outer_wire: TopoDS_Wire, inner_wires: Optional[Iterable[TopoDS_Wire]] = None
) -> TopoDS_Face:
    """Make a face from an outer wire and optional inner wire(s)."""
    face_builder = BRepBuilderAPI_MakeFace(outer_wire, True)
    if inner_wires:
        for inner_wire in inner_wires:
            face_builder.Add(inner_wire)

    return face_builder.Face()


def faces_from_wire_soup(wires: Iterable[TopoDS_Wire]) -> Iterable[TopoDS_Face]:
    """Make faces from unorganized, possibly nested (but non-intersecting) wires."""

    wires = list(wires)
    if not are_wires_coplanar(wires):
        raise ValueError("wires not coplanar")

    def fix_wires():
        for wire in wires:
            # TODO split self intersecting wires?
            yield closed_wire(wire)

    faces = [BRepBuilderAPI_MakeFace(wire, True).Face() for wire in fix_wires()]

    if len(faces) < 2:
        yield from faces
        return

    included_in: dict[int, set[int]] = {}
    for i, face_i in enumerate(faces):
        for j, face_j in enumerate(faces):
            if i != j and BRepFeat.IsInside_s(face_i, face_j):
                included_in.setdefault(i, set()).add(j)

    WireListPair = tuple[list[TopoDS_Wire], list[TopoDS_Wire]]
    outers_and_inners: dict[int, WireListPair] = {}

    for i in range(len(faces)):
        ancestors = included_in.get(i, set())
        if len(ancestors) % 2:  # odd depth: inner ring
            parent_i = max(ancestors, key=lambda i: len(included_in.get(i, set())))
            _, inners = outers_and_inners.setdefault(parent_i, ([], []))
            inners.append(face_outer_wire(faces[i]))
        else:  # even depth: outer ring
            outers, _ = outers_and_inners.setdefault(i, ([], []))
            outers.append(face_outer_wire(faces[i]))

    for outers, inners in outers_and_inners.values():
        if len(outers) == 1:
            yield face_from_wires(outers[0], inners)
        else:  # pragma: nocover
            # shouldn't ever get here
            # but yield everything as simple faces just in case
            logger.warn("invalid nesting (found %d outer wires)", len(outers))
            for path in chain(outers, inners):
                yield face_from_wires(path)


#### wires


def is_wire_closed(wire: TopoDS_Wire) -> bool:
    """Check whether a wire is closed."""
    return BRep_Tool.IsClosed_s(wire)


def are_wires_coplanar(wires: Iterable[TopoDS_Wire]) -> bool:
    """Check whether wires are coplanar."""
    wires = list(wires)
    return (
        not wires or BRepLib_FindSurface(make_compound(wires), OnlyPlane=True).Found()
    )


def wire_from_continuous_edges(
    edges: Iterable[TopoDS_Edge], *, closed: bool = False
) -> TopoDS_Wire:
    """Make a single wire from known-continuous edges;
    with no reordering nor any checking."""
    extend = ShapeExtend_WireData(TopoDS_Wire())
    for edge in edges:
        extend.AddOriented(edge, 0)

    wire = extend.Wire()
    return closed_wire(wire) if closed else wire


def closed_wire(wire: TopoDS_Wire) -> TopoDS_Wire:
    """Ensure a wire is closed.
    - if already closed return it untouched.
    - otherwise return a new wire with a closing segment appended."""

    if is_wire_closed(wire):
        return wire

    it = topoDS_iterator(wire)
    try:
        first_edge = next(it)
        last_edge = next(it)
        while True:
            try:
                last_edge = next(it)
            except StopIteration:
                break
    except StopIteration:
        # wire has fewer that 2 edges
        return wire

    adaptor = BRepAdaptor_Curve(TopoDS.Edge_s(first_edge))
    start = adaptor.Value(adaptor.FirstParameter())

    adaptor = BRepAdaptor_Curve(TopoDS.Edge_s(last_edge))
    end = adaptor.Value(adaptor.LastParameter())

    if not start.IsEqual(end, _TOLERANCE):
        extend = ShapeExtend_WireData()
        extend.AddOriented(wire, 0)
        extend.AddOriented(edge_from_curve(segment_curve(end, start)), 0)
        wire = extend.Wire()

    fix = ShapeFix_Wire(wire, TopoDS_Face(), _TOLERANCE)
    fix.FixClosed()
    fix.FixConnected()
    return fix.Wire()


#### edges


def edge_from_curve(curve: Geom_Curve) -> TopoDS_Edge:
    return BRepBuilderAPI_MakeEdge(curve).Edge()


def edge_to_curve(edge: TopoDS_Edge):
    return BRepAdaptor_Curve(edge)


#### curves


BEZIER_MAX_DEGREE = Geom_BezierCurve.MaxDegree_s()

CurveOrAdaptor = Union[Geom_Curve, GeomAdaptor_Curve, BRepAdaptor_Curve]


def segment_curve(start: PntLike, end: PntLike) -> Geom_TrimmedCurve:
    try:
        return GC_MakeSegment(as_Pnt(start), as_Pnt(end)).Value()
    except (StdFail_NotDone, Standard_Failure) as e:
        raise ValueError(f"could not make segment curve from {start}, {end}", e)


def bezier_curve(*controls: PntLike) -> Geom_BezierCurve:
    n = len(controls)
    if not 2 <= n <= BEZIER_MAX_DEGREE:
        raise ValueError(
            f"bezier curve must have between 2 and {BEZIER_MAX_DEGREE} control points"
        )

    poles = TColgp_Array1OfPnt(1, n)
    for i, control in enumerate(controls, 1):
        poles.SetValue(i, as_Pnt(control))

    return Geom_BezierCurve(poles)


def circle_curve(
    radius: float,
    start_angle: float = 360,
    end_angle: float = 360,
    *,
    clockwise: bool = False,
    center: VecLike = (0, 0, 0),
    normal: DirLike = (0, 0, 1),
) -> Union[Geom_Circle, Geom_TrimmedCurve]:
    circle_gp = gp_Circ(gp_Ax2(gp_Pnt(), as_Dir(normal)), radius)

    if start_angle == end_angle:
        circle = GC_MakeCircle(circle_gp).Value()
    else:
        circle = GC_MakeArcOfCircle(
            circle_gp, radians(start_angle), radians(end_angle), clockwise
        ).Value()

    trsf = gp_Trsf()
    trsf.SetTranslation(as_Vec(center))
    circle.Transform(trsf)

    return circle


def ellipse_curve(
    major_radius: float,
    minor_radius: float,
    start_angle: float = 360,
    end_angle: float = 360,
    *,
    clockwise: bool = False,
    rotation: float = 0,
    center: VecLike = (0, 0, 0),
    normal: DirLike = (0, 0, 1),
) -> Union[Geom_Ellipse, Geom_TrimmedCurve]:
    if minor_radius > major_radius:
        major_radius, minor_radius = minor_radius, major_radius
        rotation += 90

    ellipse_gp = gp_Elips(
        gp_Ax2(gp_Pnt(), as_Dir(normal)), major_radius, minor_radius
    ).Rotated(gp_Ax1(), radians(rotation))
    if start_angle == end_angle:
        ellipse = GC_MakeEllipse(ellipse_gp).Value()
    else:
        ellipse = GC_MakeArcOfEllipse(
            ellipse_gp,
            radians(start_angle),
            radians(end_angle),
            clockwise,
        ).Value()

    trsf = gp_Trsf()
    trsf.SetTranslation(as_Vec(center))
    ellipse.Transform(trsf)

    return ellipse


def curve_to_bspline(curve: Geom_Curve) -> Geom_BSplineCurve:
    return GeomConvert.CurveToBSplineCurve_s(curve)  # type: ignore


def curve_to_beziers(
    curve_or_adaptor: CurveOrAdaptor,
    *,
    tolerance: float,
    max_degree: int = 3,
    max_segments: int = 100,
) -> Iterator[Geom_BezierCurve]:
    adaptor = curve_adaptor(curve_or_adaptor)
    curve_type = adaptor.GetType()

    if curve_type == GeomAbs_CurveType.GeomAbs_Line:
        start = adaptor.Value(adaptor.FirstParameter())
        end = adaptor.Value(adaptor.FirstParameter())
        yield bezier_curve(start, end)

    elif curve_type == GeomAbs_CurveType.GeomAbs_BezierCurve:
        bezier = adaptor.Bezier()
        if bezier.Degree() > max_degree or bezier.IsRational():
            yield from bspline_to_beziers(
                curve_to_bspline(bezier),
                max_degree=max_degree,
                max_segments=max_segments,
                tolerance=tolerance,
            )
        else:
            yield bezier

    elif curve_type == GeomAbs_CurveType.GeomAbs_BSplineCurve:
        yield from bspline_to_beziers(
            adaptor.BSpline(),
            max_degree=max_degree,
            max_segments=max_segments,
            tolerance=tolerance,
        )

    else:
        curve_or_adaptor = (
            adaptor.Curve()
            if isinstance(adaptor, GeomAdaptor_Curve)
            else adaptor.Curve().Curve()
        )
        yield from bspline_to_beziers(
            GeomConvert.CurveToBSplineCurve_s(curve_or_adaptor),  # type: ignore
            max_degree=max_degree,
            max_segments=max_segments,
            tolerance=tolerance,
        )


def bspline_to_beziers(
    bspline: Geom_BSplineCurve,
    *,
    tolerance: float,
    max_degree: int = 3,
    max_segments: int = 100,
) -> Iterator[Geom_BezierCurve]:
    if bspline.Degree() > 3 or bspline.IsRational():
        approx = GeomConvert_ApproxCurve(
            bspline,
            tolerance,
            GeomAbs_Shape.GeomAbs_C0,
            MaxSegments=max_segments,
            MaxDegree=max_degree,
        )
        if approx.IsDone() and approx.HasResult():
            bspline = approx.Curve()
        else:
            raise ValueError(f"could not approximate b-spline {bspline}")

    bez_convert = GeomConvert_BSplineCurveToBezierCurve(bspline)
    for i in range(bez_convert.NbArcs()):
        yield bez_convert.Arc(i + 1)


def curve_to_polyline(
    curve_or_adaptor: CurveOrAdaptor,
    *,
    tolerance: float,
) -> Iterator[gp_Pnt]:
    adaptor = curve_adaptor(curve_or_adaptor)

    start = adaptor.FirstParameter()
    end = adaptor.LastParameter()

    curve_type = adaptor.GetType()
    if curve_type == GeomAbs_CurveType.GeomAbs_Line:
        yield adaptor.Value(start)
        yield adaptor.Value(end)
    else:
        points = GCPnts_QuasiUniformDeflection(adaptor, tolerance, start, end)
        if points.IsDone():
            for i in range(points.NbPoints()):
                yield points.Value(i + 1)
        else:
            raise ValueError("could not convert to polyline")


def curve_adaptor(curve_or_adaptor: CurveOrAdaptor):
    return (
        GeomAdaptor_Curve(curve_or_adaptor)
        if isinstance(curve_or_adaptor, Geom_Curve)
        else curve_or_adaptor
    )
