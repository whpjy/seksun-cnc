import type { ManufacturingFeature } from "./types";

export function featureMarkerColor(feature: ManufacturingFeature) {
  if (feature.kind === "hole") return 0x4f7cff;
  if (feature.kind === "pocket") return 0xf0a23b;
  if (feature.kind === "slot") return 0xa66be0;
  if (feature.kind === "planar_surface") return 0x24a8b8;
  if ("source" in feature && feature.source === "rotational") {
    if (feature.kind.includes("groove")) return 0xe0588b;
    if (feature.kind === "inner_bore" || feature.kind === "inner_taper") return 0x35b779;
    return 0xe5b43b;
  }
  return 0x18b89a;
}

export function featureDisplayName(feature: ManufacturingFeature) {
  if (feature.kind === "hole") return feature.end_type === "through" ? "通孔" : feature.end_type === "blind" ? "盲孔" : "孔候选";
  if (feature.kind === "pocket") return "封闭型腔";
  if (feature.kind === "slot") return "贯通槽";
  if (feature.kind === "planar_surface") return "平面铣削区域";
  if (feature.kind === "internal_profile") return feature.machining_kind === "engraving" ? "浅雕刻" : "内部轮廓";
  if (feature.kind === "external_groove_candidate") return "外圆槽候选";
  if (feature.kind === "internal_groove_candidate") return "内圆槽候选";
  if (feature.kind === "cylindrical_land") return "外圆段";
  if (feature.kind === "inner_bore") return "内孔段";
  if (feature.kind === "inner_taper") return "内锥段";
  if (feature.kind === "taper") return "外锥段";
  if (feature.kind === "radial_transition") return "圆弧/台阶过渡";
  if (feature.kind === "thread_form_candidate") return "螺纹形态候选";
  return "切断边界";
}

export function featureDimensionLabel(feature: ManufacturingFeature) {
  if ("source" in feature && feature.source === "rotational") {
    return `Ø${feature.diameter.toFixed(2)} × ${feature.width_mm.toFixed(2)} mm`;
  }
  if ("diameter" in feature) return `Ø${feature.diameter.toFixed(2)} × ${feature.length.toFixed(2)} mm`;
  return `${feature.length.toFixed(2)} × ${feature.width.toFixed(2)} mm`;
}
