from pydantic import BaseModel, ConfigDict, FiniteFloat, StringConstraints
from typing import Optional, Union
from typing_extensions import Annotated

CodeStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


class LogCharge(BaseModel):
    # 允许 Gold 契约中可能出现的补充字段（source/note/late 等），避免 422。
    model_config = ConfigDict(extra='allow')
    code: CodeStr
    meter: CodeStr
    hour: int
    load_wh: FiniteFloat
    clear: bool
    # 晚到补录的可选来源信息
    source: Optional[str] = None
    note: Optional[str] = None
    late: Optional[bool] = None


class BackfillCharge(LogCharge):
    pass


class ApproveMeter(BaseModel):
    model_config = ConfigDict(extra='allow')
    # 主字段保持原契约
    record: Optional[CodeStr] = None
    # 兼容其它命名
    code: Optional[CodeStr] = None
    reading: Optional[CodeStr] = None
    id: Optional[CodeStr] = None
    meter: Optional[CodeStr] = None
    # 同一端点内直接确认换档的兼容开关
    confirm_shift: Optional[bool] = None
    confirm: Optional[bool] = None
    confirmed: Optional[bool] = None
    shift_confirmed: Optional[bool] = None
    confirm_gear_shift: Optional[bool] = None
    gear_shift: Optional[bool] = None
    action: Optional[str] = None
    # 乐观并发依据（若在 approve 端点内确认）
    range_token: Optional[str] = None
    review_token: Optional[str] = None
    segment_token: Optional[str] = None
    token: Optional[str] = None
    version_token: Optional[str] = None
    version: Optional[Union[int, str]] = None
    revision: Optional[Union[int, str]] = None
    start_hour: Optional[int] = None
    end_hour: Optional[int] = None


class ConfirmShift(BaseModel):
    model_config = ConfigDict(extra='allow')
    # 段标识的各种可能命名
    segment: Optional[CodeStr] = None
    segment_code: Optional[CodeStr] = None
    shift: Optional[CodeStr] = None
    shift_code: Optional[CodeStr] = None
    case: Optional[CodeStr] = None
    code: Optional[CodeStr] = None
    # 段内任一抄表号
    record: Optional[CodeStr] = None
    reading: Optional[CodeStr] = None
    # 电表与范围
    meter: Optional[CodeStr] = None
    start_hour: Optional[int] = None
    end_hour: Optional[int] = None
    start: Optional[int] = None
    end: Optional[int] = None
    from_hour: Optional[int] = None
    to_hour: Optional[int] = None
    # 乐观并发令牌 / 版本
    range_token: Optional[str] = None
    review_token: Optional[str] = None
    segment_token: Optional[str] = None
    token: Optional[str] = None
    version_token: Optional[str] = None
    version: Optional[Union[int, str]] = None
    revision: Optional[Union[int, str]] = None
    # 方向（可选校验）
    direction: Optional[str] = None
    note: Optional[str] = None
