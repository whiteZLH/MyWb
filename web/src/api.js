/**
 * 与后端 /admin/api/* 的通信层。
 *
 * 约定：
 * - 所有请求带 same-origin 凭据（管理会话 Cookie）。
 * - 非 GET 请求带 X-CSRF-Token（登录后由 session/login 返回，保存在 store）。
 * - 401 时统一触发登出（回登录页），与旧实现行为一致。
 */

let csrf = "";
let onUnauthorized = () => {};

export function setCsrf(value) {
  csrf = value || "";
}

export function getCsrf() {
  return csrf;
}

export function setUnauthorizedHandler(handler) {
  onUnauthorized = handler;
}

export async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = { "Content-Type": "application/json" };
  if (method !== "GET") headers["X-CSRF-Token"] = csrf;

  const response = await fetch("/admin/api/" + path, {
    method,
    headers,
    credentials: "same-origin",
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });

  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("服务器暂时不可用，请稍后重试");
  }

  if (!response.ok) {
    if (response.status === 401 && path !== "login") onUnauthorized();
    throw new Error(typeof data.detail === "string" ? data.detail : "操作失败，请检查输入后重试");
  }
  return data;
}
