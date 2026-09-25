/** 通用格式化工具（与旧前端保持一致的展示口径）。 */

export function stamp(ts) {
  return ts ? new Date(ts).toLocaleString("zh-CN", { hour12: false }) : "未提供";
}

export function number(value) {
  return Number(value ?? 0).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

export function rate(value) {
  return value === null || value === undefined ? "—" : value.toFixed(1) + "%";
}

export function seconds(ms) {
  return ms === null || ms === undefined ? "—" : (ms / 1000).toFixed(2) + " s";
}

/** 账号池状态 → 展示文案与颜色。 */
export const POOL_STATES = {
  available: ["可用", "green"],
  cooling: ["冷却中", "amber"],
  paused: ["已暂停", ""],
  exhausted: ["积分耗尽", "amber"],
  invalid: ["凭据异常", "red"],
};

/** 请求结果 → 展示文案。 */
export const OUTCOMES = {
  success: "完成",
  stream_error: "流式错误",
  interrupted: "未完整结束",
  http_error: "请求失败",
};
