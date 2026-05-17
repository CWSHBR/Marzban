export const getBaseApi = () =>
  window.__MARZBAN_CONFIG__?.baseApi || import.meta.env.VITE_BASE_API || "/api/";
