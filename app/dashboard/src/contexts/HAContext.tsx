import { useQuery } from "react-query";
import { fetch } from "service/http";
import { queryClient } from "utils/react-query";
import { z } from "zod";

export const HARoleSchema = z.enum(["leader", "follower"]);

export const HASelfSchema = z.object({
  panel_id: z.string(),
  role: HARoleSchema,
  epoch: z.number(),
  ha_enabled: z.boolean(),
  local_core_enabled: z.boolean(),
  fencing_seconds_left: z.number().nullable().optional(),
  leader_for_seconds: z.number().nullable().optional(),
  last_error: z.string().nullable().optional(),
});

export const HALeaseSchema = z.object({
  holder: z.string().nullable().optional(),
  epoch: z.number(),
  advertise_url: z.string().nullable().optional(),
  acquired_at: z.string().nullable().optional(),
  renewed_at: z.string().nullable().optional(),
  expires_at: z.string().nullable().optional(),
  expires_in: z.number().nullable().optional(),
  stepdown_requested: z.boolean(),
});

export const HAMemberSchema = z.object({
  panel_id: z.string(),
  role: HARoleSchema,
  version: z.string().nullable().optional(),
  advertise_url: z.string().nullable().optional(),
  local_core_enabled: z.boolean(),
  started_at: z.string().nullable().optional(),
  last_seen: z.string().nullable().optional(),
  last_seen_age: z.number().nullable().optional(),
  is_stale: z.boolean(),
  is_self: z.boolean(),
});

export const HAOutboxSchema = z.object({
  // `pending` and `oldest_age_seconds` are null when the panel could not read
  // the queue, which is a different thing from an empty queue.
  pending: z.number().nullable().optional(),
  oldest_created_at: z.string().nullable().optional(),
  oldest_age_seconds: z.number().nullable().optional(),
  backlog_bailout: z.boolean(),
});

export const HANodesSchema = z.object({
  by_status: z.record(z.number()),
  needs_restart: z.number(),
});

const HAStatusBodySchema = z.object({
  self: HASelfSchema,
  lease: HALeaseSchema.nullable().optional(),
  members: z.array(HAMemberSchema),
  outbox: HAOutboxSchema,
  notifications: HAOutboxSchema,
  nodes: HANodesSchema,
  can_handover: z.boolean(),
  handover_target: z.string().nullable().optional(),
  handover_blocked_reason: z.string().nullable().optional(),
});

// `self` is reserved in Python, so the panel snapshot may arrive under either
// `self` or the internal `self_` name depending on how the response is
// serialized. Normalize before validating so the UI only deals with `self`.
const normalizeSelfKey = (payload: unknown): unknown => {
  if (
    payload &&
    typeof payload === "object" &&
    !Array.isArray(payload) &&
    "self_" in payload &&
    !("self" in payload)
  ) {
    const { self_: self, ...rest } = payload as Record<string, unknown>;
    return { ...rest, self };
  }
  return payload;
};

export const HAStatusSchema = z.preprocess(normalizeSelfKey, HAStatusBodySchema);

export const HAStepdownResultSchema = z.object({
  requested: z.boolean(),
  target: z.string().nullable().optional(),
});

export type HARoleType = z.infer<typeof HARoleSchema>;
export type HASelfType = z.infer<typeof HASelfSchema>;
export type HALeaseType = z.infer<typeof HALeaseSchema>;
export type HAMemberType = z.infer<typeof HAMemberSchema>;
export type HAOutboxType = z.infer<typeof HAOutboxSchema>;
export type HANodesType = z.infer<typeof HANodesSchema>;
export type HAStatusType = z.infer<typeof HAStatusSchema>;
export type HAStepdownResultType = z.infer<typeof HAStepdownResultSchema>;

export const FetchHAStatusQueryKey = "ha-status";

// A member whose heartbeat is older than this is not a safe handover target,
// mirroring HANDOVER_STALE_AFTER on the panel.
export const HA_MEMBER_STALE_AFTER = 15;

export const fetchHAStatus = (): Promise<HAStatusType> =>
  fetch("/ha/status").then((status) => HAStatusSchema.parse(status));

export const useHAStatus = () => {
  return useQuery({
    queryKey: FetchHAStatusQueryKey,
    queryFn: fetchHAStatus,
    refetchInterval: 5000,
    refetchOnWindowFocus: false,
  });
};

export const requestStepdown = (): Promise<HAStepdownResultType> => {
  return fetch("/ha/stepdown", { method: "POST" })
    .then((result) => HAStepdownResultSchema.parse(result))
    .finally(() => {
      queryClient.invalidateQueries(FetchHAStatusQueryKey);
    });
};

export const isMemberStale = (member: HAMemberType): boolean =>
  member.is_stale ||
  member.last_seen_age === null ||
  member.last_seen_age === undefined ||
  member.last_seen_age > HA_MEMBER_STALE_AFTER;
