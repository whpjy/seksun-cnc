/** Schematic vector silhouettes; these describe tool families, not measured assemblies. */
import { toolKindLabel } from "./toolKindLabels";

export function ToolKindIcon({ kind }: { kind: string }) {
  const common = { fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
  let shape;
  switch (kind) {
    case "turning_od":
      shape = <><path d="M8 10h23v8H18v18H8z" /><path d="m18 20 7 7-7 7-7-7z" fill="currentColor" opacity=".22" /><path d="m18 20 7 7-7 7-7-7z" /></>;
      break;
    case "turning_id":
      shape = <><path d="M8 20h24v7H8z" /><path d="M32 20h5v4l-5 3z" fill="currentColor" opacity=".25" /><path d="M32 20h5v4l-5 3z" /><path d="M12 23h15" /></>;
      break;
    case "grooving":
      shape = <><path d="M10 8h27v9H25v14h-6V17h-9z" /><path d="M19 31v5h6v-5" /><path d="M12 13h22" /></>;
      break;
    case "internal_grooving":
      shape = <><path d="M8 19h27v7H8z" /><path d="M29 26v9h5v-9" /><path d="M12 22h17" /></>;
      break;
    case "cutoff":
      shape = <><path d="M8 9h27v8H23v20h-7V17H8z" /><path d="M16 37h7" /><path d="M12 13h20" /></>;
      break;
    case "threading":
      shape = <><path d="M8 10h26v8H20v11H8z" /><path d="m20 29-5 8-5-8" fill="currentColor" opacity=".22" /><path d="m20 29-5 8-5-8" /><path d="m28 25 3 3 3-3 3 3" /></>;
      break;
    case "drill":
      shape = <><path d="M17 6h14v20l-7 14-7-14z" /><path d="M17 17c5 1 9 5 14 6M17 24c5 1 8 5 11 6" /><path d="M24 7v8" /></>;
      break;
    case "end_mill":
      shape = <><path d="M17 6h14v15H17zM17 21h14v18H17z" /><path d="M17 26h14M20 39V27m8 12V27" /><path d="M19 34h4m4-5h4" /></>;
      break;
    case "ball_end_mill":
      shape = <><path d="M17 6h14v15H17zM17 21h14v12a7 7 0 0 1-14 0z" /><path d="M17 26h14M20 34l8-8M23 39l8-8" /></>;
      break;
    case "chamfer_mill":
      shape = <><path d="M18 6h12v17l8 12H10l8-12z" /><path d="M15 31h18M20 11h8" /></>;
      break;
    case "face_mill":
      shape = <><path d="M20 6h8v17h-8zM8 23h32v11H8z" /><path d="M11 34v5m8-5v5m10-5v5m8-5v5" /><path d="M14 28h20" /></>;
      break;
    case "tap":
      shape = <><path d="M18 6h12v12H18zM19 18h10v19l-5 4-5-4z" /><path d="M19 22h10m-10 4h10m-10 4h10m-10 4h10" /></>;
      break;
    default:
      shape = <><path d="M16 7h16v26H16zM13 33h22v5H13z" /><path d="M20 13h8m-8 6h8m-8 6h8" /></>;
  }
  return <span className="tool-kind-icon" data-kind={kind} title={toolKindLabel(kind)}>
    <svg viewBox="0 0 48 48" aria-label={toolKindLabel(kind)} role="img" {...common}>{shape}</svg>
  </span>;
}
