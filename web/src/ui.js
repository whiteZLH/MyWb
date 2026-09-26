/**
 * 对话框与全局 UI 开关。用模块级响应式状态承载，便于任意层级的
 * 组件直接触发（旧实现靠全局函数 + DOM id 查找）。
 */
import { reactive } from "vue";

export const ui = reactive({
  accountOpen: false,
  keyOpen: false,
  confirm: {
    open: false,
    title: "",
    desc: "",
    rename: null,
    number: null,
    numberLabel: "",
    error: "",
    submit: null,
  },
});

export function openAccount() {
  ui.accountOpen = true;
}

export function openKeyDialog() {
  ui.keyOpen = true;
}

/**
 * @param {object} options
 * @param {string} options.title 标题
 * @param {string} options.desc 说明
 * @param {string|null} [options.rename] 传入字符串则显示备注输入框
 * @param {number|null} [options.number] 传入数字则显示数值输入框
 * @param {string} [options.numberLabel] 数值输入框标签
 * @param {() => Promise<void>} options.submit 确认后的回调
 */
export function openConfirm({ title, desc, rename = null, number = null, numberLabel = "", submit }) {
  ui.confirm.title = title;
  ui.confirm.desc = desc;
  ui.confirm.rename = rename;
  ui.confirm.number = number;
  ui.confirm.numberLabel = numberLabel;
  ui.confirm.error = "";
  ui.confirm.submit = submit;
  ui.confirm.open = true;
}

export function closeConfirm() {
  ui.confirm.open = false;
  ui.confirm.submit = null;
}
