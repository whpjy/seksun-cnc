#include <BRepAdaptor_Surface.hxx>
#include <BRepAdaptor_Curve.hxx>
#include <BRepAlgoAPI_Section.hxx>
#include <BRepBndLib.hxx>
#include <BRepGProp.hxx>
#include <BRepMesh_IncrementalMesh.hxx>
#include <BRepTools.hxx>
#include <BRep_Tool.hxx>
#include <Bnd_Box.hxx>
#include <GProp_GProps.hxx>
#include <GCPnts_QuasiUniformDeflection.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <Interface_Static.hxx>
#include <STEPControl_Reader.hxx>
#include <StlAPI_Writer.hxx>
#include <TopAbs.hxx>
#include <TopExp_Explorer.hxx>
#include <TopExp.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Shape.hxx>
#include <TopoDS_Vertex.hxx>
#include <TopoDS_Wire.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_ListIteratorOfListOfShape.hxx>
#include <gp_Ax1.hxx>
#include <gp_Cylinder.hxx>
#include <gp_Dir.hxx>
#include <gp_Pln.hxx>
#include <gp_Pnt.hxx>
#include <gp_Vec.hxx>

#include <algorithm>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <numeric>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace fs = std::filesystem;

namespace {

struct Vec3 {
    double x{};
    double y{};
    double z{};
};

struct PlanarFeature {
    std::string id;
    int source_face_index{};
    double area{};
    Vec3 center;
    Vec3 normal;
    Vec3 bounds_minimum;
    Vec3 bounds_maximum;
    int wire_count{};
    int adjacent_edge_count{};
    int rising_edge_count{};
    int falling_edge_count{};
    int level_edge_count{};
    int free_edge_count{};
};

struct CylindricalFeature {
    std::string id;
    std::string kind;
    double radius{};
    double length{};
    Vec3 center;
    Vec3 axis;
    double angular_span_degrees{};
};

struct InternalProfileFeature {
    std::string id;
    std::string source_face_id;
    int source_face_index{};
    int wire_index{};
    Vec3 center;
    Vec3 access_direction;
    Vec3 bounds_minimum;
    Vec3 bounds_maximum;
    int edge_count{};
    double perimeter{};
    bool circular{};
};

struct SolidCandidate {
    int index{};
    double volume{};
    double surface_area{};
    Vec3 center;
    Vec3 bounds_minimum;
    Vec3 bounds_maximum;
};

struct RotationalSectionPoint {
    double z{};
    double radius{};
};

struct RotationalSectionCandidate {
    std::string source_feature_id;
    Vec3 axis_origin;
    Vec3 axis;
    Vec3 plane_normal;
    std::vector<RotationalSectionPoint> outer_profile;
    std::vector<RotationalSectionPoint> inner_profile;
    double tolerance_mm{};
    std::vector<std::string> warnings;
};

using EdgePolyline = std::vector<Vec3>;

double shape_diagonal(const TopoDS_Shape& shape) {
    Bnd_Box box;
    BRepBndLib::AddOptimal(shape, box, Standard_False, Standard_False);
    double x_min, y_min, z_min, x_max, y_max, z_max;
    box.Get(x_min, y_min, z_min, x_max, y_max, z_max);
    return std::hypot(std::hypot(x_max - x_min, y_max - y_min), z_max - z_min);
}

double dot(const Vec3& left, const Vec3& right) {
    return left.x * right.x + left.y * right.y + left.z * right.z;
}

Vec3 subtract(const Vec3& left, const Vec3& right) {
    return {left.x - right.x, left.y - right.y, left.z - right.z};
}

Vec3 cross(const Vec3& left, const Vec3& right) {
    return {
        left.y * right.z - left.z * right.y,
        left.z * right.x - left.x * right.z,
        left.x * right.y - left.y * right.x,
    };
}

Vec3 normalize(const Vec3& value) {
    const double length = std::sqrt(dot(value, value));
    if (length <= 1e-12) throw std::runtime_error("Cannot normalize zero vector");
    return {value.x / length, value.y / length, value.z / length};
}

std::vector<RotationalSectionPoint> simplify_rotational_profile(
    const std::vector<RotationalSectionPoint>& points,
    double tolerance) {
    if (points.size() <= 2) return points;
    std::vector<RotationalSectionPoint> result{points.front()};
    for (std::size_t index = 1; index + 1 < points.size(); ++index) {
        const auto& previous = result.back();
        const auto& current = points[index];
        const auto& following = points[index + 1];
        const double left_z = current.z - previous.z;
        const double right_z = following.z - current.z;
        if (std::abs(left_z) <= tolerance || std::abs(right_z) <= tolerance) {
            result.push_back(current);
            continue;
        }
        const double left_slope = (current.radius - previous.radius) / left_z;
        const double right_slope = (following.radius - current.radius) / right_z;
        if (std::abs(left_slope - right_slope) > tolerance) result.push_back(current);
    }
    result.push_back(points.back());
    return result;
}

std::vector<EdgePolyline> collect_visual_edges(const TopoDS_Shape& shape) {
    const double deflection = std::clamp(shape_diagonal(shape) / 10000.0, 0.005, 0.025);

    std::vector<EdgePolyline> result;
    TopTools_IndexedMapOfShape unique_edges;
    TopExp::MapShapes(shape, TopAbs_EDGE, unique_edges);
    result.reserve(static_cast<std::size_t>(unique_edges.Extent()));
    for (int edge_index = 1; edge_index <= unique_edges.Extent(); ++edge_index) {
        const TopoDS_Edge edge = TopoDS::Edge(unique_edges(edge_index));
        BRepAdaptor_Curve curve(edge);
        GCPnts_QuasiUniformDeflection points(curve, deflection);
        if (!points.IsDone() || points.NbPoints() < 2) continue;
        EdgePolyline polyline;
        const int stride = std::max(1, static_cast<int>(std::ceil(points.NbPoints() / 2048.0)));
        for (int index = 1; index <= points.NbPoints(); index += stride) {
            const gp_Pnt point = points.Value(index);
            polyline.push_back({point.X(), point.Y(), point.Z()});
        }
        const gp_Pnt last_point = points.Value(points.NbPoints());
        const Vec3 last{last_point.X(), last_point.Y(), last_point.Z()};
        if (polyline.empty() || std::abs(polyline.back().x - last.x) > 1e-9
            || std::abs(polyline.back().y - last.y) > 1e-9
            || std::abs(polyline.back().z - last.z) > 1e-9) {
            polyline.push_back(last);
        }
        if (polyline.size() >= 2) result.push_back(std::move(polyline));
    }
    return result;
}

TopoDS_Shape select_primary_solid(
    const TopoDS_Shape& shape,
    int requested_solid_index,
    int& source_solid_count,
    int& selected_solid_index,
    std::vector<SolidCandidate>& candidates) {
    source_solid_count = 0;
    selected_solid_index = 0;
    double largest_volume = std::numeric_limits<double>::lowest();
    TopoDS_Shape largest_solid;
    for (TopExp_Explorer explorer(shape, TopAbs_SOLID); explorer.More(); explorer.Next()) {
        ++source_solid_count;
        const TopoDS_Shape candidate = explorer.Current();
        GProp_GProps volume_properties;
        BRepGProp::VolumeProperties(candidate, volume_properties);
        const double volume = std::abs(volume_properties.Mass());
        GProp_GProps surface_properties;
        BRepGProp::SurfaceProperties(candidate, surface_properties);
        Bnd_Box candidate_box;
        BRepBndLib::AddOptimal(candidate, candidate_box, Standard_False, Standard_False);
        double x_min, y_min, z_min, x_max, y_max, z_max;
        candidate_box.Get(x_min, y_min, z_min, x_max, y_max, z_max);
        candidates.push_back({
            source_solid_count,
            volume,
            std::abs(surface_properties.Mass()),
            {volume_properties.CentreOfMass().X(), volume_properties.CentreOfMass().Y(), volume_properties.CentreOfMass().Z()},
            {x_min, y_min, z_min},
            {x_max, y_max, z_max},
        });
        if (requested_solid_index == source_solid_count) {
            selected_solid_index = source_solid_count;
            largest_solid = candidate;
        }
        if (requested_solid_index > 0) continue;
        if (largest_solid.IsNull() || volume > largest_volume) {
            largest_solid = candidate;
            largest_volume = volume;
            selected_solid_index = source_solid_count;
        }
    }
    if (requested_solid_index > 0 && selected_solid_index == 0) {
        throw std::runtime_error("Requested solid index is out of range");
    }
    return largest_solid.IsNull() ? shape : largest_solid;
}

int count_subshapes(const TopoDS_Shape& shape, TopAbs_ShapeEnum type) {
    int count = 0;
    for (TopExp_Explorer explorer(shape, type); explorer.More(); explorer.Next()) {
        ++count;
    }
    return count;
}

Vec3 point_to_vec(const gp_Pnt& point) {
    return {point.X(), point.Y(), point.Z()};
}

Vec3 dir_to_vec(const gp_Dir& direction) {
    return {direction.X(), direction.Y(), direction.Z()};
}

Vec3 face_center(const TopoDS_Face& face) {
    GProp_GProps properties;
    BRepGProp::SurfaceProperties(face, properties);
    return point_to_vec(properties.CentreOfMass());
}

double face_area(const TopoDS_Face& face) {
    GProp_GProps properties;
    BRepGProp::SurfaceProperties(face, properties);
    return properties.Mass();
}

std::pair<Vec3, Vec3> face_bounds(const TopoDS_Face& face) {
    Bnd_Box box;
    BRepBndLib::AddOptimal(face, box, Standard_False, Standard_False);
    double x_min, y_min, z_min, x_max, y_max, z_max;
    box.Get(x_min, y_min, z_min, x_max, y_max, z_max);
    return {{x_min, y_min, z_min}, {x_max, y_max, z_max}};
}

double cylinder_length(const TopoDS_Face& face, const gp_Ax1& axis) {
    double minimum = std::numeric_limits<double>::max();
    double maximum = std::numeric_limits<double>::lowest();
    bool found = false;
    const gp_Pnt origin = axis.Location();
    const gp_Dir direction = axis.Direction();
    for (TopExp_Explorer explorer(face, TopAbs_VERTEX); explorer.More(); explorer.Next()) {
        const gp_Pnt point = BRep_Tool::Pnt(TopoDS::Vertex(explorer.Current()));
        const double projection =
            (point.X() - origin.X()) * direction.X()
            + (point.Y() - origin.Y()) * direction.Y()
            + (point.Z() - origin.Z()) * direction.Z();
        minimum = std::min(minimum, projection);
        maximum = std::max(maximum, projection);
        found = true;
    }
    return found ? std::max(0.0, maximum - minimum) : 0.0;
}

Vec3 cylinder_axis_center(const TopoDS_Face& face, const gp_Ax1& axis) {
    double minimum = std::numeric_limits<double>::max();
    double maximum = std::numeric_limits<double>::lowest();
    bool found = false;
    const gp_Pnt origin = axis.Location();
    const gp_Dir direction = axis.Direction();
    for (TopExp_Explorer explorer(face, TopAbs_VERTEX); explorer.More(); explorer.Next()) {
        const gp_Pnt point = BRep_Tool::Pnt(TopoDS::Vertex(explorer.Current()));
        const double projection =
            (point.X() - origin.X()) * direction.X()
            + (point.Y() - origin.Y()) * direction.Y()
            + (point.Z() - origin.Z()) * direction.Z();
        minimum = std::min(minimum, projection);
        maximum = std::max(maximum, projection);
        found = true;
    }
    const double middle = found ? (minimum + maximum) / 2.0 : 0.0;
    return {
        origin.X() + direction.X() * middle,
        origin.Y() + direction.Y() * middle,
        origin.Z() + direction.Z() * middle,
    };
}

std::vector<PlanarFeature> collect_planar_features(const TopoDS_Shape& shape) {
    std::vector<PlanarFeature> features;
    TopTools_IndexedDataMapOfShapeListOfShape edge_faces;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, edge_faces);
    int planar_index = 1;
    int source_face_index = 0;
    for (TopExp_Explorer explorer(shape, TopAbs_FACE); explorer.More(); explorer.Next()) {
        ++source_face_index;
        const TopoDS_Face face = TopoDS::Face(explorer.Current());
        const BRepAdaptor_Surface surface(face, Standard_True);
        if (surface.GetType() != GeomAbs_Plane) continue;
        gp_Dir normal = surface.Plane().Axis().Direction();
        if (face.Orientation() == TopAbs_REVERSED) normal.Reverse();
        const auto [minimum, maximum] = face_bounds(face);
        const Vec3 center = face_center(face);
        const Vec3 normal_vector = dir_to_vec(normal);
        int adjacent_edges = 0;
        int rising_edges = 0;
        int falling_edges = 0;
        int level_edges = 0;
        int free_edges = 0;
        for (TopExp_Explorer edge_explorer(face, TopAbs_EDGE); edge_explorer.More(); edge_explorer.Next()) {
            const TopoDS_Shape edge = edge_explorer.Current();
            if (!edge_faces.Contains(edge)) {
                ++free_edges;
                continue;
            }
            bool has_other_face = false;
            for (TopTools_ListIteratorOfListOfShape iterator(edge_faces.FindFromKey(edge)); iterator.More(); iterator.Next()) {
                const TopoDS_Face other = TopoDS::Face(iterator.Value());
                if (face.IsSame(other)) continue;
                has_other_face = true;
                const Vec3 other_center = face_center(other);
                const double height =
                    (other_center.x - center.x) * normal_vector.x
                    + (other_center.y - center.y) * normal_vector.y
                    + (other_center.z - center.z) * normal_vector.z;
                ++adjacent_edges;
                if (height > 0.05) ++rising_edges;
                else if (height < -0.05) ++falling_edges;
                else ++level_edges;
            }
            if (!has_other_face) ++free_edges;
        }
        features.push_back({
            "PF-" + std::to_string(planar_index++),
            source_face_index,
            face_area(face),
            center,
            normal_vector,
            minimum,
            maximum,
            count_subshapes(face, TopAbs_WIRE),
            adjacent_edges,
            rising_edges,
            falling_edges,
            level_edges,
            free_edges,
        });
    }
    std::sort(
        features.begin(),
        features.end(),
        [](const PlanarFeature& left, const PlanarFeature& right) {
            return left.area > right.area;
        });
    return features;
}

std::vector<CylindricalFeature> collect_cylindrical_features(
    const TopoDS_Shape& shape) {
    std::vector<CylindricalFeature> features;
    int index = 1;
    for (TopExp_Explorer explorer(shape, TopAbs_FACE); explorer.More(); explorer.Next()) {
        const TopoDS_Face face = TopoDS::Face(explorer.Current());
        const BRepAdaptor_Surface surface(face, Standard_True);
        if (surface.GetType() != GeomAbs_Cylinder) continue;
        const gp_Cylinder cylinder = surface.Cylinder();
        const gp_Ax1 axis = cylinder.Axis();
        const bool reversed = face.Orientation() == TopAbs_REVERSED;
        const double angular_span_degrees = std::min(
            360.0,
            std::abs(surface.LastUParameter() - surface.FirstUParameter())
                * 180.0 / std::acos(-1.0));
        features.push_back({
            "CF-" + std::to_string(index++),
            reversed ? "hole" : "boss",
            cylinder.Radius(),
            cylinder_length(face, axis),
            cylinder_axis_center(face, axis),
            dir_to_vec(axis.Direction()),
            angular_span_degrees,
        });
    }
    return features;
}

std::optional<RotationalSectionCandidate> collect_rotational_section(
    const TopoDS_Shape& shape,
    const std::vector<CylindricalFeature>& cylinders) {
    if (cylinders.empty()) return std::nullopt;
    const auto reference = std::max_element(
        cylinders.begin(), cylinders.end(),
        [](const CylindricalFeature& left, const CylindricalFeature& right) {
            const auto score = [](const CylindricalFeature& item) {
                const double kind_factor = item.kind == "hole" ? 0.25 : 1.0;
                const double span_factor = std::max(item.angular_span_degrees / 360.0, 0.1);
                return kind_factor * item.radius * std::max(item.length, 0.1) * span_factor;
            };
            return score(left) < score(right);
        });
    if (reference == cylinders.end()) return std::nullopt;

    const Vec3 axis = normalize(reference->axis);
    const Vec3 helper = std::abs(axis.x) < 0.8 ? Vec3{1, 0, 0} : Vec3{0, 1, 0};
    const Vec3 plane_normal = normalize(cross(axis, helper));
    const Vec3 radial_direction = normalize(cross(plane_normal, axis));
    const gp_Pln plane(
        gp_Pnt(reference->center.x, reference->center.y, reference->center.z),
        gp_Dir(plane_normal.x, plane_normal.y, plane_normal.z));
    BRepAlgoAPI_Section section(shape, plane, Standard_False);
    section.Approximation(Standard_True);
    section.Build();
    if (!section.IsDone()) return std::nullopt;

    const double diagonal = shape_diagonal(shape);
    const double sampling_tolerance = std::clamp(diagonal / 20000.0, 0.001, 0.01);
    std::vector<RotationalSectionPoint> samples;
    TopTools_IndexedMapOfShape section_edges;
    TopExp::MapShapes(section.Shape(), TopAbs_EDGE, section_edges);
    for (int edge_index = 1; edge_index <= section_edges.Extent(); ++edge_index) {
        const TopoDS_Edge edge = TopoDS::Edge(section_edges(edge_index));
        BRepAdaptor_Curve curve(edge);
        GCPnts_QuasiUniformDeflection points(curve, sampling_tolerance);
        if (!points.IsDone() || points.NbPoints() < 2) continue;
        const int stride = std::max(1, static_cast<int>(std::ceil(points.NbPoints() / 1024.0)));
        for (int index = 1; index <= points.NbPoints(); index += stride) {
            const Vec3 point = point_to_vec(points.Value(index));
            const Vec3 delta = subtract(point, reference->center);
            samples.push_back({dot(delta, axis), std::abs(dot(delta, radial_direction))});
        }
        const Vec3 point = point_to_vec(points.Value(points.NbPoints()));
        const Vec3 delta = subtract(point, reference->center);
        samples.push_back({dot(delta, axis), std::abs(dot(delta, radial_direction))});
    }
    if (samples.size() < 2) return std::nullopt;

    std::sort(samples.begin(), samples.end(), [](const auto& left, const auto& right) {
        if (std::abs(left.z - right.z) > 1e-12) return left.z < right.z;
        return left.radius < right.radius;
    });
    const double z_tolerance = std::max(sampling_tolerance, diagonal * 1e-6);
    const double radius_tolerance = std::max(sampling_tolerance, diagonal * 1e-6);
    std::vector<std::vector<RotationalSectionPoint>> groups;
    for (const auto& sample : samples) {
        if (groups.empty()) {
            groups.push_back({sample});
            continue;
        }
        const double mean_z = std::accumulate(
            groups.back().begin(), groups.back().end(), 0.0,
            [](double sum, const RotationalSectionPoint& item) { return sum + item.z; })
            / static_cast<double>(groups.back().size());
        if (std::abs(sample.z - mean_z) > z_tolerance) groups.push_back({sample});
        else groups.back().push_back(sample);
    }

    std::vector<RotationalSectionPoint> outer;
    std::vector<RotationalSectionPoint> inner;
    int inner_groups = 0;
    for (const auto& group : groups) {
        const double z = std::accumulate(
            group.begin(), group.end(), 0.0,
            [](double sum, const RotationalSectionPoint& item) { return sum + item.z; })
            / static_cast<double>(group.size());
        std::vector<double> radii;
        for (const auto& item : group) {
            if (item.radius <= radius_tolerance) continue;
            if (radii.empty() || std::all_of(radii.begin(), radii.end(), [&](double radius) {
                    return std::abs(radius - item.radius) > radius_tolerance;
                })) {
                radii.push_back(item.radius);
            }
        }
        if (radii.empty()) continue;
        const auto [minimum, maximum] = std::minmax_element(radii.begin(), radii.end());
        outer.push_back({z, *maximum});
        if (*maximum - *minimum > radius_tolerance * 2) {
            inner.push_back({z, *minimum});
            ++inner_groups;
        }
    }
    if (outer.size() < 2) return std::nullopt;
    outer = simplify_rotational_profile(outer, sampling_tolerance);
    if (inner_groups < 2) inner.clear();
    else inner = simplify_rotational_profile(inner, sampling_tolerance);

    RotationalSectionCandidate result{
        reference->id,
        reference->center,
        axis,
        plane_normal,
        std::move(outer),
        std::move(inner),
        sampling_tolerance,
        {},
    };
    if (!result.inner_profile.empty()) {
        result.warnings.push_back(
            "Inner section envelope may include grooves or radial-hole intersections and requires review");
    }
    return result;
}

std::vector<InternalProfileFeature> collect_internal_profiles(
    const TopoDS_Shape& shape,
    const std::vector<PlanarFeature>& planes) {
    std::vector<InternalProfileFeature> features;
    int source_face_index = 0;
    for (TopExp_Explorer explorer(shape, TopAbs_FACE); explorer.More(); explorer.Next()) {
        ++source_face_index;
        const TopoDS_Face face = TopoDS::Face(explorer.Current());
        const BRepAdaptor_Surface surface(face, Standard_True);
        if (surface.GetType() != GeomAbs_Plane) continue;
        const auto plane = std::find_if(
            planes.begin(), planes.end(),
            [source_face_index](const PlanarFeature& item) {
                return item.source_face_index == source_face_index;
            });
        if (plane == planes.end()) continue;
        gp_Dir normal = surface.Plane().Axis().Direction();
        if (face.Orientation() == TopAbs_REVERSED) normal.Reverse();
        const TopoDS_Wire outer = BRepTools::OuterWire(face);
        int wire_index = 0;
        for (TopExp_Explorer wire_explorer(face, TopAbs_WIRE); wire_explorer.More(); wire_explorer.Next()) {
            ++wire_index;
            const TopoDS_Wire wire = TopoDS::Wire(wire_explorer.Current());
            if (!outer.IsNull() && wire.IsSame(outer)) continue;
            GProp_GProps linear_properties;
            BRepGProp::LinearProperties(wire, linear_properties);
            Bnd_Box box;
            BRepBndLib::AddOptimal(wire, box, Standard_False, Standard_False);
            if (box.IsVoid()) continue;
            double x_min, y_min, z_min, x_max, y_max, z_max;
            box.Get(x_min, y_min, z_min, x_max, y_max, z_max);
            bool circular = true;
            int edge_count = 0;
            for (TopExp_Explorer edge_explorer(wire, TopAbs_EDGE); edge_explorer.More(); edge_explorer.Next()) {
                ++edge_count;
                const BRepAdaptor_Curve curve(TopoDS::Edge(edge_explorer.Current()));
                if (curve.GetType() != GeomAbs_Circle) circular = false;
            }
            if (edge_count == 0) continue;
            const gp_Pnt center = linear_properties.CentreOfMass();
            features.push_back({
                "IP-" + std::to_string(features.size() + 1),
                plane->id,
                source_face_index,
                wire_index,
                point_to_vec(center),
                dir_to_vec(normal),
                {x_min, y_min, z_min},
                {x_max, y_max, z_max},
                edge_count,
                std::abs(linear_properties.Mass()),
                circular,
            });
        }
    }
    return features;
}

void append_vec(std::ostringstream& json, const Vec3& value) {
    json << "{\"x\":" << value.x << ",\"y\":" << value.y
         << ",\"z\":" << value.z << '}';
}

std::string json_escape(const std::string& value) {
    std::ostringstream escaped;
    for (const unsigned char character : value) {
        switch (character) {
            case '\"': escaped << "\\\""; break;
            case '\\': escaped << "\\\\"; break;
            case '\n': escaped << "\\n"; break;
            case '\r': escaped << "\\r"; break;
            case '\t': escaped << "\\t"; break;
            default: escaped << character;
        }
    }
    return escaped.str();
}

std::string make_json(
    const fs::path& input,
    const TopoDS_Shape& shape,
    int source_solid_count,
    int selected_solid_index,
    bool selection_confirmed,
    const std::vector<SolidCandidate>& solid_candidates,
    const std::vector<PlanarFeature>& planes,
    const std::vector<CylindricalFeature>& cylinders,
    const std::vector<InternalProfileFeature>& internal_profiles,
    const std::optional<RotationalSectionCandidate>& rotational_section,
    const std::vector<EdgePolyline>& visual_edges) {
    Bnd_Box box;
    BRepBndLib::AddOptimal(shape, box, Standard_False, Standard_False);
    if (box.IsVoid()) throw std::runtime_error("Cannot calculate bounding box");
    double x_min, y_min, z_min, x_max, y_max, z_max;
    box.Get(x_min, y_min, z_min, x_max, y_max, z_max);

    GProp_GProps surface_properties;
    BRepGProp::SurfaceProperties(shape, surface_properties);
    GProp_GProps volume_properties;
    BRepGProp::VolumeProperties(shape, volume_properties);

    std::ostringstream json;
    json << std::fixed << std::setprecision(6);
    json << "{\n"
         << "  \"schema_version\": \"0.5.0\",\n"
         << "  \"source_file\": \"" << json_escape(input.filename().string()) << "\",\n"
         << "  \"topology\": {\"source_solids\": " << source_solid_count
         << ", \"selected_solid_index\": " << selected_solid_index
         << ", \"selection_confirmed\": " << (selection_confirmed ? 1 : 0)
         << ", \"selected_solids\": " << count_subshapes(shape, TopAbs_SOLID)
         << ", \"solids\": " << count_subshapes(shape, TopAbs_SOLID)
         << ", \"faces\": " << count_subshapes(shape, TopAbs_FACE)
         << ", \"edges\": " << count_subshapes(shape, TopAbs_EDGE) << "},\n"
         << "  \"measurements\": {\n"
         << "    \"surface_area\": " << surface_properties.Mass() << ",\n"
         << "    \"volume\": " << volume_properties.Mass() << ",\n"
         << "    \"bounding_box\": {\"minimum\":";
    append_vec(json, {x_min, y_min, z_min});
    json << ",\"maximum\":";
    append_vec(json, {x_max, y_max, z_max});
    json << ",\"size\":";
    append_vec(json, {x_max - x_min, y_max - y_min, z_max - z_min});
    json << "}\n  },\n";

    json << "  \"solid_candidates\": [";
    for (std::size_t index = 0; index < solid_candidates.size(); ++index) {
        const auto& candidate = solid_candidates[index];
        if (index == 0) json << '\n';
        json << "    {\"index\":" << candidate.index
             << ",\"volume\":" << candidate.volume
             << ",\"surface_area\":" << candidate.surface_area
             << ",\"center\":";
        append_vec(json, candidate.center);
        json << ",\"bounds\":{\"minimum\":";
        append_vec(json, candidate.bounds_minimum);
        json << ",\"maximum\":";
        append_vec(json, candidate.bounds_maximum);
        json << ",\"size\":";
        append_vec(json, {
            candidate.bounds_maximum.x - candidate.bounds_minimum.x,
            candidate.bounds_maximum.y - candidate.bounds_minimum.y,
            candidate.bounds_maximum.z - candidate.bounds_minimum.z,
        });
        json << "},\"selected\":" << (candidate.index == selected_solid_index ? "true" : "false")
             << '}' << (index + 1 < solid_candidates.size() ? "," : "") << '\n';
    }
    json << "  ],\n";

    json << "  \"internal_profile_features\": [";
    for (std::size_t index = 0; index < internal_profiles.size(); ++index) {
        const auto& feature = internal_profiles[index];
        if (index == 0) json << '\n';
        json << "    {\"id\":\"" << feature.id
             << "\",\"source_face_id\":\"" << feature.source_face_id
             << "\",\"source_face_index\":" << feature.source_face_index
             << ",\"wire_index\":" << feature.wire_index
             << ",\"center\":";
        append_vec(json, feature.center);
        json << ",\"access_direction\":";
        append_vec(json, feature.access_direction);
        json << ",\"bounds\":{\"minimum\":";
        append_vec(json, feature.bounds_minimum);
        json << ",\"maximum\":";
        append_vec(json, feature.bounds_maximum);
        json << ",\"size\":";
        append_vec(json, {
            feature.bounds_maximum.x - feature.bounds_minimum.x,
            feature.bounds_maximum.y - feature.bounds_minimum.y,
            feature.bounds_maximum.z - feature.bounds_minimum.z,
        });
        json << "},\"edge_count\":" << feature.edge_count
             << ",\"perimeter\":" << feature.perimeter
             << ",\"circular\":" << (feature.circular ? "true" : "false")
             << '}' << (index + 1 < internal_profiles.size() ? "," : "") << '\n';
    }
    json << "  ],\n";

    json << "  \"planar_features\": [";
    for (std::size_t index = 0; index < planes.size(); ++index) {
        const auto& feature = planes[index];
        if (index == 0) json << '\n';
        json << "    {\"id\":\"" << feature.id << "\",\"source_face_index\":"
             << feature.source_face_index << ",\"area\":" << feature.area
             << ",\"center\":";
        append_vec(json, feature.center);
        json << ",\"normal\":";
        append_vec(json, feature.normal);
        json << ",\"bounds\":{\"minimum\":";
        append_vec(json, feature.bounds_minimum);
        json << ",\"maximum\":";
        append_vec(json, feature.bounds_maximum);
        json << ",\"size\":";
        append_vec(json, {
            feature.bounds_maximum.x - feature.bounds_minimum.x,
            feature.bounds_maximum.y - feature.bounds_minimum.y,
            feature.bounds_maximum.z - feature.bounds_minimum.z,
        });
        json << "},\"wire_count\":" << feature.wire_count
             << ",\"adjacent_edge_count\":" << feature.adjacent_edge_count
             << ",\"rising_edge_count\":" << feature.rising_edge_count
             << ",\"falling_edge_count\":" << feature.falling_edge_count
             << ",\"level_edge_count\":" << feature.level_edge_count
             << ",\"free_edge_count\":" << feature.free_edge_count;
        json << '}' << (index + 1 < planes.size() ? "," : "") << '\n';
    }
    json << "  ],\n";

    json << "  \"cylindrical_features\": [";
    for (std::size_t index = 0; index < cylinders.size(); ++index) {
        const auto& feature = cylinders[index];
        if (index == 0) json << '\n';
        json << "    {\"id\":\"" << feature.id << "\",\"kind\":\""
             << feature.kind << "\",\"radius\":" << feature.radius
             << ",\"diameter\":" << feature.radius * 2.0
             << ",\"length\":" << feature.length << ",\"center\":";
        append_vec(json, feature.center);
        json << ",\"axis\":";
        append_vec(json, feature.axis);
        json << ",\"angular_span_degrees\":" << feature.angular_span_degrees;
        json << '}' << (index + 1 < cylinders.size() ? "," : "") << '\n';
    }
    json << "  ],\n";

    json << "  \"rotational_sections\": [";
    if (rotational_section.has_value()) {
        const auto& section = rotational_section.value();
        json << '\n'
             << "    {\"source_feature_id\":\"" << json_escape(section.source_feature_id)
             << "\",\"axis_origin\":";
        append_vec(json, section.axis_origin);
        json << ",\"axis\":";
        append_vec(json, section.axis);
        json << ",\"plane_normal\":";
        append_vec(json, section.plane_normal);
        json << ",\"outer_profile\":[";
        for (std::size_t index = 0; index < section.outer_profile.size(); ++index) {
            const auto& point = section.outer_profile[index];
            json << "{\"z\":" << point.z << ",\"radius\":" << point.radius << '}';
            if (index + 1 < section.outer_profile.size()) json << ',';
        }
        json << "],\"inner_profile\":[";
        for (std::size_t index = 0; index < section.inner_profile.size(); ++index) {
            const auto& point = section.inner_profile[index];
            json << "{\"z\":" << point.z << ",\"radius\":" << point.radius << '}';
            if (index + 1 < section.inner_profile.size()) json << ',';
        }
        json << "],\"tolerance_mm\":" << section.tolerance_mm << ",\"warnings\":[";
        for (std::size_t index = 0; index < section.warnings.size(); ++index) {
            json << '\"' << json_escape(section.warnings[index]) << '\"';
            if (index + 1 < section.warnings.size()) json << ',';
        }
        json << "]}\n";
    }
    json << "  ],\n";

    json << "  \"visual_edges\": [";
    for (std::size_t edge_index = 0; edge_index < visual_edges.size(); ++edge_index) {
        if (edge_index == 0) json << '\n';
        json << "    [";
        const auto& edge = visual_edges[edge_index];
        for (std::size_t point_index = 0; point_index < edge.size(); ++point_index) {
            append_vec(json, edge[point_index]);
            if (point_index + 1 < edge.size()) json << ',';
        }
        json << ']' << (edge_index + 1 < visual_edges.size() ? "," : "") << '\n';
    }
    json << "  ]\n}\n";
    return json.str();
}

void write_file(const fs::path& output, const std::string& content) {
    if (!output.parent_path().empty()) fs::create_directories(output.parent_path());
    std::ofstream stream(output, std::ios::binary);
    if (!stream) throw std::runtime_error("Cannot open output file");
    stream << content;
}

}  // namespace

int main(int argc, char* argv[]) {
    if (argc != 4 && argc != 5) {
        std::cerr << "Usage: occt-analyzer <input.step> <analysis.json> <model.stl> [solid-index]\n";
        return 2;
    }
    try {
        const fs::path input = argv[1];
        const fs::path output = argv[2];
        const fs::path mesh_output = argv[3];
        const int requested_solid_index = argc == 5 ? std::stoi(argv[4]) : 0;
        if (requested_solid_index < 0) throw std::runtime_error("Solid index cannot be negative");
        if (!fs::is_regular_file(input)) throw std::runtime_error("STEP file not found");

        Interface_Static::SetCVal("xstep.cascade.unit", "MM");
        STEPControl_Reader reader;
        if (reader.ReadFile(input.string().c_str()) != IFSelect_RetDone) {
            throw std::runtime_error("OCCT could not read the STEP file");
        }
        if (reader.TransferRoots() <= 0) throw std::runtime_error("STEP has no roots");
        const TopoDS_Shape transferred_shape = reader.OneShape();
        if (transferred_shape.IsNull()) throw std::runtime_error("STEP produced an empty shape");
        int source_solid_count = 0;
        int selected_solid_index = 0;
        std::vector<SolidCandidate> solid_candidates;
        const TopoDS_Shape shape = select_primary_solid(
            transferred_shape,
            requested_solid_index,
            source_solid_count,
            selected_solid_index,
            solid_candidates);

        // The manufacturing viewport, feature analysis and downstream CAM must
        // all use the same selected solid.  Showing the complete assembly here
        // makes a successful selection look unchanged and can mislead the user
        // into approving operations for a gasket, pin or reference component.
        // Keep zoomed-in fillets and holes close to the underlying STEP
        // surfaces.  The size-relative linear tolerance avoids over-tessellating
        // tiny parts while the tighter angular tolerance removes visible facets
        // from cylindrical faces.
        const double mesh_deflection = std::clamp(shape_diagonal(shape) / 8000.0, 0.008, 0.04);
        constexpr double mesh_angular_deflection = 0.06;
        BRepMesh_IncrementalMesh mesh(shape, mesh_deflection, false, mesh_angular_deflection, true);
        mesh.Perform();
        if (!mesh.IsDone()) throw std::runtime_error("Cannot triangulate STEP model");
        if (!mesh_output.parent_path().empty()) fs::create_directories(mesh_output.parent_path());
        StlAPI_Writer writer;
        writer.ASCIIMode() = Standard_False;
        if (!writer.Write(shape, mesh_output.string().c_str())) {
            throw std::runtime_error("Cannot write STL model");
        }

        const auto planes = collect_planar_features(shape);
        const auto cylinders = collect_cylindrical_features(shape);
        const auto internal_profiles = collect_internal_profiles(shape, planes);
        const auto rotational_section = collect_rotational_section(shape, cylinders);
        const auto visual_edges = collect_visual_edges(shape);
        write_file(output, make_json(
            input, shape, source_solid_count, selected_solid_index,
            requested_solid_index > 0, solid_candidates,
            planes, cylinders, internal_profiles, rotational_section, visual_edges));
        std::cout << "Analyzed " << input.filename().string() << ": "
                  << planes.size() << " planes, " << cylinders.size()
                  << " cylinders, " << internal_profiles.size()
                  << " internal profiles, " << (rotational_section.has_value() ? 1 : 0)
                  << " rotational sections, " << visual_edges.size() << " visual edges\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "ERROR: " << error.what() << '\n';
        return 1;
    }
}
