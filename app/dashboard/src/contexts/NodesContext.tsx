import { useQuery } from "react-query";
import { fetch } from "service/http";
import { z } from "zod";
import { create } from "zustand";
import { FilterUsageType, useDashboard } from "./DashboardContext";

export const NodeSchema = z.object({
  name: z.string().min(1),
  address: z.string().min(1),
  port: z
    .number()
    .min(1)
    .or(z.string().transform((v) => parseFloat(v))),
  api_port: z
    .number()
    .min(1)
    .or(z.string().transform((v) => parseFloat(v))),
  xray_version: z.string().nullable().optional(),
  id: z.number().nullable().optional(),
  status: z
    .enum(["connected", "connecting", "error", "disabled"])
    .nullable()
    .optional(),
  message: z.string().nullable().optional(),
  add_as_new_host: z.boolean().optional(),
  usage_coefficient: z.number().or(z.string().transform((v) => parseFloat(v))),
  has_custom_config: z.boolean().nullable().optional(),
  config_pending: z.boolean().nullable().optional(),
});

export type NodeType = z.infer<typeof NodeSchema>;

export type NodeConfigInbound = {
  tag: string;
  protocol: string;
  port?: number | string | null;
  network?: string | null;
  tls?: string | null;
  origin: "shared" | "override" | "node_only";
  changed: string[];
};

export type NodeConfigWarning = {
  code: "client_params_differ" | "node_tag_conflict" | string;
  tag: string;
  fields: string[];
  node?: string | null;
  message: string;
};

export type NodeConfigSummary = {
  inbounds: NodeConfigInbound[];
  dropped_tags: string[];
  warnings: NodeConfigWarning[];
  new_tags: string[];
};

export type NodeConfigResponse = {
  node_id: number;
  custom: boolean;
  config: Record<string, unknown>;
  shared_config: Record<string, unknown>;
  updated_at?: string | null;
  updated_by?: string | null;
  pending?: boolean | null;
  summary?: NodeConfigSummary | null;
};

export const nodeConfigApi = {
  get: (id: number) => fetch<NodeConfigResponse>(`/node/${id}/config`),
  validate: (id: number, config: unknown) =>
    fetch<NodeConfigSummary>(`/node/${id}/config/validate`, {
      method: "POST",
      body: config as Record<string, unknown>,
    }),
  save: (id: number, config: unknown, restart: boolean) =>
    fetch<NodeConfigResponse>(`/node/${id}/config`, {
      method: "PUT",
      body: config as Record<string, unknown>,
      query: { restart },
    }),
  reset: (id: number, restart: boolean) =>
    fetch<NodeConfigResponse>(`/node/${id}/config`, {
      method: "DELETE",
      query: { restart },
    }),
  restart: (id: number) =>
    fetch(`/node/${id}/restart`, {
      method: "POST",
    }),
};

export const getNodeDefaultValues = (): NodeType => ({
  name: "",
  address: "",
  port: 62050,
  api_port: 62051,
  xray_version: "",
  usage_coefficient: 1,
});

export const FetchNodesQueryKey = "fetch-nodes-query-key";

export type NodeStore = {
  nodes: NodeType[];
  addNode: (node: NodeType) => Promise<unknown>;
  fetchNodes: () => Promise<NodeType[]>;
  fetchNodesUsage: (query: FilterUsageType) => Promise<void>;
  updateNode: (node: NodeType) => Promise<unknown>;
  reconnectNode: (node: NodeType) => Promise<unknown>;
  deletingNode?: NodeType | null;
  deleteNode: () => Promise<unknown>;
  setDeletingNode: (node: NodeType | null) => void;
};

export const useNodesQuery = () => {
  const { isEditingNodes, isEditingCore } = useDashboard();
  return useQuery({
    queryKey: FetchNodesQueryKey,
    queryFn: useNodes.getState().fetchNodes,
    // The core settings dialog lists nodes with their "needs restart" state.
    refetchInterval: isEditingNodes ? 3000 : isEditingCore ? 5000 : undefined,
    refetchOnWindowFocus: false,
  });
};

export const useNodes = create<NodeStore>((set, get) => ({
  nodes: [],
  addNode(body) {
    return fetch("/node", { method: "POST", body });
  },
  fetchNodes() {
    return fetch("/nodes");
  },
  fetchNodesUsage(query: FilterUsageType) {
    return fetch("/nodes/usage", { query });
  },
  updateNode(body) {
    return fetch(`/node/${body.id}`, {
      method: "PUT",
      body,
    });
  },
  setDeletingNode(node) {
    set({ deletingNode: node });
  },
  reconnectNode(body) {
    return fetch(`/node/${body.id}/reconnect`, {
      method: "POST",
    });
  },
  deleteNode: () => {
    return fetch(`/node/${get().deletingNode?.id}`, {
      method: "DELETE",
    });
  },
}));
