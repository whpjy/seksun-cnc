export type SideHoleToolSettings = {enabled:boolean;bodyEnabled:boolean;pointAngle:string;extension:string;flute:string;stickout:string;shank:string;holderDiameter:string;holderLength:string;station:string;evidence:string};
export const emptySideHoleTool:SideHoleToolSettings={enabled:false,bodyEnabled:false,pointAngle:"",extension:"0",flute:"",stickout:"",shank:"",holderDiameter:"",holderLength:"",station:"gang",evidence:""};
export function drillGeometryRequest(settings:SideHoleToolSettings) {
  if(!settings.enabled)return null;
  const angle=Number(settings.pointAngle),extension=Number(settings.extension);
  if(!settings.pointAngle || !Number.isFinite(angle) || angle<90 || angle>150)throw new Error("请填写实际钻尖角（90–150°）");
  if(settings.extension==="" || !Number.isFinite(extension) || extension< -20 || extension>20)throw new Error("请填写完整直径终点偏移（-20–20 mm）");
  if(!settings.evidence.trim())throw new Error("请填写刀具几何及钻深依据");
  const body:Record<string,number>={};
  if(settings.bodyEnabled) {
    for(const [key,value] of Object.entries({flute_length_mm:settings.flute,stickout_from_tip_mm:settings.stickout,shank_diameter_mm:settings.shank,holder_diameter_mm:settings.holderDiameter,holder_length_mm:settings.holderLength})) {
      if(!value || !Number.isFinite(Number(value)) || Number(value)<=0)throw new Error("刀杆与刀柄检查需完整填写五项正数尺寸");body[key]=Number(value);
    }
  }
  return {point_angle_deg:angle,full_diameter_extension_mm:extension,station_group:settings.station,evidence:settings.evidence,...body};
}

export function SideHoleToolInputs({value,onChange,disabled}:{value:SideHoleToolSettings;onChange:(value:SideHoleToolSettings)=>void;disabled:boolean}) {
  const change=(key:keyof SideHoleToolSettings,newValue:string|boolean)=>onChange({...value,[key]:newValue});
  return <div className="side-hole-tool-inputs"><label className="side-hole-check-toggle"><input type="checkbox" disabled={disabled} checked={value.enabled} onChange={e=>change("enabled",e.target.checked)}/>加入钻尖与钻深检查</label>{value.enabled && <>
    <p className="stock-note">填写实际刀具资料；未填写的角度和尺寸不会自动采用默认值。当前仍是几何试算。</p>
    <label>钻尖角（°）<input type="number" min="90" max="150" step="any" disabled={disabled} value={value.pointAngle} onChange={e=>change("pointAngle",e.target.value)}/></label>
    <label>完整直径终点偏移（mm，正值更深）<input type="number" min="-20" max="20" step="any" disabled={disabled} value={value.extension} onChange={e=>change("extension",e.target.value)}/></label><p className="stock-note">以 CAD 圆柱段终点为零，负值回退。钻尖会继续向前延伸；需同时检查成品侵入和圆柱段残料。</p>
    <label>使用刀位组<select disabled={disabled} value={value.station} onChange={e=>change("station",e.target.value)}><option value="gang">正面排刀</option><option value="back">背面刀位</option></select></label>
    <label className="side-hole-check-toggle"><input type="checkbox" disabled={disabled} checked={value.bodyEnabled} onChange={e=>change("bodyEnabled",e.target.checked)}/>同时检查刀杆与刀柄</label>{value.bodyEnabled && <>
      <p className="stock-note">刃长和伸出长度均从钻尖量起，伸出长度量到刀柄前端；其余为简化圆柱包络尺寸。</p>
      {([["flute","刃长（mm）"],["stickout","从钻尖到刀柄的伸出长度（mm）"],["shank","刀杆直径（mm）"],["holderDiameter","刀柄直径（mm）"],["holderLength","刀柄长度（mm）"]] as const).map(([key,label])=><label key={key}>{label}<input disabled={disabled} type="number" min="0.001" step="any" value={value[key]} onChange={e=>change(key,e.target.value)}/></label>)}
    </>}
    <label>刀具几何及钻深依据<textarea disabled={disabled} maxLength={1000} value={value.evidence} onChange={e=>change("evidence",e.target.value)}/></label>
  </>}</div>;
}
