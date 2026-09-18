export function toolKindLabel(kind: string): string {
  const labels: Record<string, string> = {
    turning_od: "外圆车刀", turning_id: "内孔车刀", grooving: "外切槽刀",
    internal_grooving: "内切槽刀", cutoff: "切断刀", threading: "螺纹刀",
    drill: "钻头", end_mill: "立铣刀", ball_end_mill: "球头铣刀",
    chamfer_mill: "倒角刀", face_mill: "面铣刀", tap: "丝锥",
  };
  return labels[kind] ?? "其他刀具";
}
