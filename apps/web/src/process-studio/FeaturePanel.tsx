import { useMemo, useState } from "react";
import { Crosshair } from "lucide-react";
import type { ManufacturingFeature } from "../types";
import { featureDimensionLabel, featureDisplayName, featureMarkerColor } from "../featurePresentation";

const categories = ["全部", "孔", "平面", "型腔与槽", "外轮廓", "内轮廓"] as const;
type Category = typeof categories[number];
function categoryOf(feature: ManufacturingFeature): Category {
  if (feature.kind === "hole") return "孔";
  if (feature.kind === "planar_surface") return "平面";
  if (feature.kind === "pocket" || feature.kind === "slot") return "型腔与槽";
  if (feature.kind === "internal_profile" || feature.kind.startsWith("inner_") || feature.kind.startsWith("internal_") || feature.id.includes("INNER")) return "内轮廓";
  return "外轮廓";
}

export default function FeaturePanel({ features, selectedIds, onSelect, onShowAll }: {
  features: ManufacturingFeature[]; selectedIds: string[]; onSelect: (id: string) => void; onShowAll: () => void;
}) {
  const [category, setCategory] = useState<Category>("全部");
  const entries = useMemo(() => features.map((feature, index) => ({ feature, marker: `F${String(index + 1).padStart(2, "0")}`, category: categoryOf(feature) })), [features]);
  const filtered = entries.filter(item => category === "全部" || item.category === category);
  function showAll() { setCategory("全部"); onShowAll(); }
  return <section className="studio-feature-panel">
    <div className="studio-feature-controls">
      <div className="studio-feature-filters" aria-label="特征分类">{categories.map(item => <button key={item} aria-pressed={category === item} className={category === item ? "active" : ""} onClick={() => setCategory(item)}>{item}<span>{item === "全部" ? entries.length : entries.filter(entry => entry.category === item).length}</span></button>)}</div>
      <div className="studio-feature-summary"><span>显示 {filtered.length} / {features.length} 项</span><button title="显示全部特征" onClick={showAll}><Crosshair size={12} />显示全部</button></div>
    </div>
    <div className="studio-feature-list">{filtered.map(({ feature, marker }) => <button key={feature.id} className={`studio-feature-card${selectedIds.includes(feature.id) ? " selected" : ""}`} aria-pressed={selectedIds.includes(feature.id)} onClick={() => onSelect(feature.id)} style={{ borderLeftColor: `#${featureMarkerColor(feature).toString(16).padStart(6, "0")}` }}>
      <div className="studio-feature-card-title"><strong><b>{marker}</b>{featureDisplayName(feature)}</strong><span className={feature.review_state === "review" ? "review" : "accepted"}>{feature.review_state === "review" ? "待核对" : "已识别"}</span></div>
      <div className="studio-feature-dimensions"><span>尺寸</span><strong>{featureDimensionLabel(feature)}</strong></div>
    </button>)}{!filtered.length && <p className="studio-panel-empty">{features.length ? "该分类暂无制造特征" : "等待几何特征解析结果"}</p>}</div>
  </section>;
}
