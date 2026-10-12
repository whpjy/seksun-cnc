"""Customer policy snapshots: defaults are proposed, never customer-approved."""
import json
from threading import RLock

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .stock_selection import rule_path

router = APIRouter()
_lock = RLock()


class MachiningPolicy(BaseModel):
    finishing_allowance_mm: float | None = Field(default=None, ge=0, le=5, allow_inf_nan=False)
    customer_confirmed: bool = False
    evidence: str = Field(default="", max_length=1000)
    revision: int = Field(default=1, ge=1)


def load_machining_policy():
    path = rule_path().with_name("machining-policy.json")
    with _lock:
        return MachiningPolicy.model_validate(json.loads(path.read_text(encoding="utf-8"))) if path.is_file() else MachiningPolicy()


@router.get("/api/v1/process-rules/machining")
def get_machining_policy():
    return load_machining_policy()


@router.put("/api/v1/process-rules/machining")
def save_machining_policy(request: MachiningPolicy):
    from fastapi import HTTPException
    if request.customer_confirmed and (request.finishing_allowance_mm is None or not request.evidence.strip()):
        raise HTTPException(422, "确认企业余量规则时，请填写单边精加工余量及客户依据")
    with _lock:
        saved = request.model_copy(update={"revision": load_machining_policy().revision + 1})
        path = rule_path().with_name("machining-policy.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(saved.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(path)
    return saved


def operation_policy_parameters(definition_id, parameters, snapshot):
    policy = MachiningPolicy.model_validate(snapshot or {})
    result = dict(parameters)
    if policy.customer_confirmed and policy.finishing_allowance_mm is not None and definition_id in {"turn_od_roughing", "turn_id_roughing"}:
        result.setdefault("stock_allowance_mm", policy.finishing_allowance_mm)
    return result


FEATURE_PROCESS_LOGIC = [
    {"feature": "同轴内孔", "steps": ["实心状态先钻底孔", "有残料且需分层时粗镗", "按图纸尺寸和公差精镗"], "tools": ["轴向钻头", "内孔粗车刀", "内孔精车刀"], "conditions": ["实际孔径满足刀具进入空间", "已有材料去除证据", "公差和参数待工程师审核"]},
    {"feature": "外圆与台阶", "steps": ["比较当前残料与精加工余量", "必要时粗车", "精车"], "tools": ["外圆粗车刀", "外圆精车刀"], "conditions": ["不得重复切削已经加工的轮廓", "刀具角度和方向可达"]},
    {"feature": "外圆槽", "steps": ["核对图纸是否仍要求该槽", "核对前序轮廓是否已形成", "必要时切槽"], "tools": ["外槽刀"], "conditions": ["槽宽、深度匹配刀宽及伸出", "确认修订差异", "不得由特征数量直接生成重复工序"]},
    {"feature": "径向孔", "steps": ["确认动力刀具与分度能力", "分度定位", "钻孔", "按图纸要求处理孔口"], "tools": ["动力钻头"], "conditions": ["每个孔需独立空间定位", "旋转轮廓仿真不能验证径向孔"]},
]
