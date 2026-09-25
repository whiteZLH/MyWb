/**
 * 复制到剪贴板，失败时给出可操作的提示（与旧实现一致）。
 */
export async function copy(value) {
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {
    return false;
  }
}
