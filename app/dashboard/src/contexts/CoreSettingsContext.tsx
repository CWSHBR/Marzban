import { fetch } from "service/http";
import { create } from "zustand";

/** Which config the core settings dialog shows: the shared one or a node's. */
export type CoreConfigTarget = "shared" | number;

type CoreSettingsStore = {
  target: CoreConfigTarget;
  setTarget: (target: CoreConfigTarget) => void;
  isLoading: boolean;
  isPostLoading: boolean;
  fetchCoreSettings: () => void;
  updateConfig: (json: string) => Promise<void>;
  restartCore: () => Promise<void>;
  version: string | null;
  started: boolean | null;
  logs_websocket: string | null;
  config: string;
};

export const useCoreSettings = create<CoreSettingsStore>((set) => ({
  target: "shared",
  setTarget: (target) => set({ target }),
  isLoading: true,
  isPostLoading: false,
  version: null,
  started: false,
  logs_websocket: null,
  config: "",
  fetchCoreSettings: () => {
    set({ isLoading: true });
    Promise.all([
      fetch("/core").then(({ version, started, logs_websocket }) =>
        set({ version, started, logs_websocket })
      ),
      fetch("/core/config").then((config) => set({ config })),
    ]).finally(() => set({ isLoading: false }));
  },
  updateConfig: (body) => {
    set({ isPostLoading: true });
    return fetch("/core/config", { method: "PUT", body }).finally(() => {
      set({ isPostLoading: false });
    });
  },
  restartCore: () => {
    return fetch("/core/restart", { method: "POST" });
  },
}));
