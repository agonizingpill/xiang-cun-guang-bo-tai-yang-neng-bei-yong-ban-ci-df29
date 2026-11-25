import {workspace} from '../workspace.js';

const page = {
  "title": "班记抄表与见证",
  "module": "蓄电班记与测量",
  "caption": "蓄电班记与测量",
  "detail": "同表有足够的更早审签读数时，用 Fourier+OLS 预测用量并给出历史误差带：超带抄数先挂起，孤立超带记为尖峰；连续至少三小时同向超带形成待审换档段，复核员确认换档后整段审签并进入拟合。晚到补录会自动补齐或扩展待审段；若确认所依据的待审范围已被补录改变，会被退回重审。下表“判定”列可查每笔抄数的结论与范围令牌。",
  "path": "/battery_logs",
  "columns": [
    {"name": "code", "label": "编号"},
    {"name": "meter", "label": "电表编号"},
    {"name": "hour", "label": "两日内整点"},
    {"name": "load_wh", "label": "用量 Wh"},
    {"name": "clear", "label": "天空晴"},
    {"name": "verified", "label": "抄表审核"},
    {"name": "verdict_label", "label": "判定"},
    {"name": "direction_label", "label": "偏离方向"},
    {"name": "predicted_wh", "label": "预测 Wh"},
    {"name": "band_wh", "label": "误差带 ±Wh"},
    {"name": "range_token", "label": "待审范围令牌"},
    {"name": "meter_revision", "label": "复核版本"}
  ],
  "layout": "stack",
  "view": "table",
  "actions": [
    {
      "name": "log_charge",
      "label": "登记整点电量",
      "path": "/battery_logs/log-charge",
      "fields": [
        {"name": "code", "label": "抄表号", "kind": "text", "initial": "WH-RECHECK"},
        {"name": "meter", "label": "电表", "kind": "text", "initial": "BAT-11"},
        {"name": "hour", "label": "两日内小时", "kind": "int", "initial": 7},
        {"name": "load_wh", "label": "整点用量 Wh", "kind": "float", "initial": 88.4},
        {"name": "clear", "label": "晴天", "kind": "bool", "initial": true}
      ],
      "help": "抄表保留班记来源；登记后立即按更早的已审签读数做预测判定，超带即挂起。",
      "roles": ["operator", "reviewer"],
      "success": "已写入"
    },
    {
      "name": "backfill",
      "label": "晚到抄数补录",
      "path": "/battery_logs/backfill",
      "fields": [
        {"name": "code", "label": "抄表号", "kind": "text", "initial": "WH-LATE"},
        {"name": "meter", "label": "电表", "kind": "text", "initial": "BAT-11"},
        {"name": "hour", "label": "晚到整点（更早小时）", "kind": "int", "initial": 6},
        {"name": "load_wh", "label": "整点用量 Wh", "kind": "float", "initial": 88.4},
        {"name": "clear", "label": "晴天", "kind": "bool", "initial": true}
      ],
      "help": "晚到记录同样参与连续性构造；若判为同向超带，可能补齐或延长待审换档段并刷新范围令牌。",
      "roles": ["operator", "reviewer"],
      "success": "补录已写入"
    },
    {
      "name": "approve_meter",
      "label": "核对班记读数",
      "path": "/battery_logs/approve-meter",
      "fields": [
        {"name": "record", "label": "抄表号", "kind": "text", "initial": "WH-RECHECK"}
      ],
      "help": "复核预测：误差带内或历史基线读数予以签认；超带读数保持挂起，连续三小时同向超带时给出待审换档段令牌。",
      "roles": ["reviewer"],
      "success": "已写入"
    },
    {
      "name": "confirm_shift",
      "label": "确认换档",
      "path": "/battery_logs/confirm-shift",
      "fields": [
        {"name": "record", "label": "段内任一抄表号", "kind": "text", "initial": "WH-RECHECK", "optional": true},
        {"name": "meter", "label": "电表编号", "kind": "text", "initial": "BAT-11", "optional": true},
        {"name": "start_hour", "label": "段起小时", "kind": "int", "initial": "", "optional": true},
        {"name": "end_hour", "label": "段止小时", "kind": "int", "initial": "", "optional": true},
        {"name": "range_token", "label": "待审范围令牌", "kind": "text", "initial": "", "optional": true},
        {"name": "revision", "label": "复核版本", "kind": "int", "initial": "", "optional": true}
      ],
      "help": "优先填段内抄表号或电表+起止小时，并粘贴表中当前“待审范围令牌/版本”。若补录已改变待审范围，会返回 409“待审范围已变”，刷新表格用新令牌重新提交即可。",
      "roles": ["reviewer"],
      "success": "换档已确认"
    }
  ]
};

export const {title} = page;
export const key = "battery_logs";
export async function render(root, role) {
  await workspace(root, role, page);
}
