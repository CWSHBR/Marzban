import {
  Alert,
  AlertDescription,
  AlertIcon,
  Badge,
  Box,
  Button,
  ButtonGroup,
  chakra,
  Flex,
  HStack,
  Menu,
  MenuButton,
  MenuItem,
  MenuList,
  ModalBody,
  ModalFooter,
  Spinner,
  Text,
  Tooltip,
  useToast,
  VStack,
} from "@chakra-ui/react";
import {
  ArrowPathIcon,
  ArrowUturnLeftIcon,
  ChevronDownIcon,
  DocumentDuplicateIcon,
} from "@heroicons/react/24/outline";
import {
  FetchNodesQueryKey,
  nodeConfigApi,
  NodeConfigInbound,
  NodeConfigResponse,
  NodeConfigSummary,
  NodeConfigWarning,
  NodeType,
} from "contexts/NodesContext";
import dayjs from "dayjs";
import debounce from "lodash.debounce";
import { FC, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "react-query";
import { countChanges, diffLines, toHunks } from "utils/lineDiff";
import { generateErrorMessage } from "utils/toastHandler";
import { JsonEditor } from "./JsonEditor";

const iconStyle = { baseStyle: { w: 4, h: 4 } };
const RestartIcon = chakra(ArrowPathIcon, iconStyle);
const ResetIcon = chakra(ArrowUturnLeftIcon, iconStyle);
const CopyIcon = chakra(DocumentDuplicateIcon, iconStyle);
const ChevronIcon = chakra(ChevronDownIcon, iconStyle);

const pretty = (value: unknown) => JSON.stringify(value, null, 2);

/** Same content regardless of key order or whitespace. */
const canonical = (value: unknown): string =>
  JSON.stringify(value, (_key, v) =>
    v && typeof v === "object" && !Array.isArray(v)
      ? Object.keys(v)
          .sort()
          .reduce<Record<string, unknown>>((sorted, key) => {
            sorted[key] = v[key];
            return sorted;
          }, {})
      : v
  );

const originScheme: Record<NodeConfigInbound["origin"], string> = {
  shared: "gray",
  override: "orange",
  node_only: "purple",
};

const InboundRow: FC<{ inbound: NodeConfigInbound }> = ({ inbound }) => {
  const { t } = useTranslation();
  const details = [
    inbound.protocol,
    inbound.network,
    inbound.tls && inbound.tls !== "none" ? inbound.tls : null,
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <Box
      w="full"
      px={2}
      py={1.5}
      borderRadius="md"
      border="1px solid"
      borderColor="gray.200"
      _dark={{ borderColor: "gray.600" }}
    >
      <HStack justifyContent="space-between" alignItems="flex-start" gap={2}>
        <Text
          fontFamily="mono"
          fontSize="xs"
          fontWeight="semibold"
          wordBreak="break-all"
        >
          {inbound.tag}
        </Text>
        <Tooltip
          placement="top"
          label={t(`nodes.config.originHint.${inbound.origin}`)}
        >
          <Badge
            colorScheme={originScheme[inbound.origin]}
            flexShrink={0}
            textTransform="none"
            fontSize="0.65rem"
          >
            {t(`nodes.config.origin.${inbound.origin}`)}
          </Badge>
        </Tooltip>
      </HStack>
      <Text fontSize="xs" opacity={0.7}>
        {details}
        {inbound.port ? ` · :${inbound.port}` : ""}
      </Text>
      {inbound.origin === "override" && inbound.changed.length > 0 && (
        <Text fontSize="xs" color="orange.500" _dark={{ color: "orange.300" }}>
          {t("nodes.config.changedFields", {
            fields: inbound.changed.join(", "),
          })}
        </Text>
      )}
    </Box>
  );
};

const warningText = (
  warning: NodeConfigWarning,
  t: (key: string, options?: Record<string, unknown>) => string
) => {
  const fields = warning.fields.join(", ");
  if (warning.code === "client_params_differ")
    return t("nodes.config.warning.clientParamsDiffer", {
      tag: warning.tag,
      fields,
    });
  if (warning.code === "node_tag_conflict")
    return t("nodes.config.warning.nodeTagConflict", {
      tag: warning.tag,
      fields,
      node: warning.node,
    });
  return warning.message;
};

const SummaryPanel: FC<{
  summary: NodeConfigSummary | null;
  error: string | null;
  isValidating: boolean;
}> = ({ summary, error, isValidating }) => {
  const { t } = useTranslation();
  return (
    <VStack alignItems="stretch" spacing={3} w="full">
      <HStack justifyContent="space-between">
        <Text fontSize="sm" fontWeight="semibold">
          {t("nodes.config.inbounds")}
          {summary ? ` · ${summary.inbounds.length}` : ""}
        </Text>
        {isValidating && <Spinner size="xs" />}
      </HStack>

      {error && (
        <Alert status="error" fontSize="xs" borderRadius="md" py={2}>
          <AlertIcon boxSize={4} />
          <AlertDescription wordBreak="break-word">{error}</AlertDescription>
        </Alert>
      )}

      {summary && (
        <>
          <VStack alignItems="stretch" spacing={1.5}>
            {summary.inbounds.map((inbound) => (
              <InboundRow key={inbound.tag} inbound={inbound} />
            ))}
          </VStack>

          {summary.dropped_tags.length > 0 && (
            <Alert status="warning" fontSize="xs" borderRadius="md" py={2}>
              <AlertIcon boxSize={4} />
              <AlertDescription wordBreak="break-word">
                {t("nodes.config.dropped", {
                  tags: summary.dropped_tags.join(", "),
                })}
              </AlertDescription>
            </Alert>
          )}

          {summary.warnings.map((warning) => (
            <Alert
              key={`${warning.code}-${warning.tag}`}
              status="warning"
              fontSize="xs"
              borderRadius="md"
              py={2}
            >
              <AlertIcon boxSize={4} />
              <AlertDescription wordBreak="break-word">
                {warningText(warning, t)}
              </AlertDescription>
            </Alert>
          ))}

          {summary.new_tags.length > 0 && (
            <Alert status="info" fontSize="xs" borderRadius="md" py={2}>
              <AlertIcon boxSize={4} />
              <AlertDescription wordBreak="break-word">
                {t("nodes.config.newTags", {
                  tags: summary.new_tags.join(", "),
                })}
              </AlertDescription>
            </Alert>
          )}
        </>
      )}
    </VStack>
  );
};

const DiffView: FC<{ shared: string; current: string }> = ({
  shared,
  current,
}) => {
  const { t } = useTranslation();
  const lines = useMemo(() => diffLines(shared, current), [shared, current]);
  const hunks = useMemo(() => toHunks(lines), [lines]);
  const { added, removed } = useMemo(() => countChanges(lines), [lines]);

  if (!hunks.length)
    return (
      <Flex h="full" alignItems="center" justifyContent="center" p={6}>
        <Text fontSize="sm" opacity={0.7} textAlign="center">
          {t("nodes.config.noDiff")}
        </Text>
      </Flex>
    );

  return (
    <Box h="full" overflow="auto" fontFamily="mono" fontSize="xs">
      <HStack
        px={3}
        py={1.5}
        position="sticky"
        top={0}
        zIndex={1}
        bg="gray.50"
        borderBottom="1px solid"
        borderColor="gray.200"
        _dark={{ bg: "gray.800", borderColor: "gray.600" }}
        fontFamily="body"
      >
        <Text opacity={0.7}>{t("nodes.config.diffLegend")}</Text>
        <Text color="green.500" fontWeight="semibold">
          +{added}
        </Text>
        <Text color="red.500" fontWeight="semibold">
          −{removed}
        </Text>
      </HStack>
      {hunks.map((hunk, index) => (
        <Box
          key={index}
          borderBottom="1px dashed"
          borderColor="gray.300"
          _dark={{ borderColor: "gray.600" }}
          py={1}
        >
          {hunk.lines.map((line, lineIndex) => (
            <Flex
              key={lineIndex}
              bg={
                line.type === "add"
                  ? "green.50"
                  : line.type === "del"
                  ? "red.50"
                  : undefined
              }
              _dark={{
                bg:
                  line.type === "add"
                    ? "rgba(72,187,120,0.16)"
                    : line.type === "del"
                    ? "rgba(245,101,101,0.16)"
                    : undefined,
              }}
            >
              <Text
                w="44px"
                flexShrink={0}
                textAlign="right"
                pr={2}
                opacity={0.45}
                userSelect="none"
              >
                {line.newNo ?? line.oldNo}
              </Text>
              <Text w="14px" flexShrink={0} userSelect="none" opacity={0.8}>
                {line.type === "add" ? "+" : line.type === "del" ? "−" : ""}
              </Text>
              <Text whiteSpace="pre" pr={3}>
                {line.text}
              </Text>
            </Flex>
          ))}
        </Box>
      ))}
    </Box>
  );
};

/**
 * Editor for one node's Xray config. Rendered inside the core settings
 * dialog, next to the shared config; brings its own ModalBody/ModalFooter.
 */
export const NodeConfigEditor: FC<{
  node: NodeType;
  onDirtyChange: (dirty: boolean) => void;
}> = ({ node, onDirtyChange }) => {
  const nodeId = node.id as number;
  const { t } = useTranslation();
  const toast = useToast();
  const queryClient = useQueryClient();
  const queryKey = ["node-config", nodeId];

  const { data, isLoading } = useQuery<NodeConfigResponse>({
    queryKey,
    queryFn: () => nodeConfigApi.get(nodeId),
    refetchOnWindowFocus: false,
    cacheTime: 0,
  });

  // `editorJson` is what the editor is told to show (a new object replaces its
  // content); `text` is what it currently holds, edits included.
  const [editorJson, setEditorJson] = useState<unknown>(null);
  const [text, setText] = useState("");
  const [view, setView] = useState<"editor" | "diff">("editor");
  const [summary, setSummary] = useState<NodeConfigSummary | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);
  const [isValidating, setValidating] = useState(false);

  const load = (config: unknown) => {
    setEditorJson(config);
    setText(pretty(config));
  };

  useEffect(() => {
    if (!data) return;
    load(data.config);
    setSummary(data.summary ?? null);
    setServerError(null);
  }, [data]);

  const parsed = useMemo(() => {
    if (!text.trim()) return { value: null, error: t("nodes.config.empty") };
    try {
      return { value: JSON.parse(text) as unknown, error: null };
    } catch (e) {
      return { value: null, error: (e as Error).message };
    }
  }, [text]);

  const currentCanonical = useMemo(
    () => (parsed.error ? null : canonical(parsed.value)),
    [parsed]
  );
  const isDirty =
    !!data && currentCanonical !== null && currentCanonical !== canonical(data.config);
  const sameAsShared =
    !!data && currentCanonical !== null && currentCanonical === canonical(data.shared_config);

  useEffect(() => {
    onDirtyChange(isDirty || (!!data && !!parsed.error));
  }, [isDirty, parsed.error, data]);

  // Ask the panel what it makes of the draft. Stale answers are dropped.
  const requestId = useRef(0);
  const validate = useMemo(
    () =>
      debounce((value: unknown) => {
        const id = ++requestId.current;
        nodeConfigApi
          .validate(nodeId, value)
          .then((result) => {
            if (id !== requestId.current) return;
            setSummary(result);
            setServerError(null);
          })
          .catch((e) => {
            if (id !== requestId.current) return;
            const detail = e?.response?._data?.detail;
            setServerError(
              typeof detail === "string" ? detail : t("nodes.config.invalid")
            );
          })
          .finally(() => {
            if (id === requestId.current) setValidating(false);
          });
      }, 500),
    [nodeId]
  );
  useEffect(() => () => validate.cancel(), [validate]);

  useEffect(() => {
    if (!data || currentCanonical === null) return;
    // The saved own config arrives with its summary; any other draft is asked about.
    if (!isDirty && data.custom && data.summary) {
      requestId.current++;
      validate.cancel();
      setSummary(data.summary);
      setServerError(null);
      setValidating(false);
      return;
    }
    setValidating(true);
    validate(parsed.value);
  }, [currentCanonical, data]);

  const onDone = (response: NodeConfigResponse, message: string) => {
    queryClient.setQueryData(queryKey, response);
    queryClient.invalidateQueries(FetchNodesQueryKey);
    toast({
      title: message,
      status: "success",
      isClosable: true,
      position: "top",
      duration: 3000,
    });
  };

  const save = useMutation(
    (restart: boolean) => nodeConfigApi.save(nodeId, parsed.value, restart),
    {
      onSuccess: (response, restart) =>
        onDone(
          response,
          t(restart ? "nodes.config.savedRestarting" : "nodes.config.saved")
        ),
      onError: (e) => {
        generateErrorMessage(e, toast);
      },
    }
  );
  const reset = useMutation(
    (restart: boolean) => nodeConfigApi.reset(nodeId, restart),
    {
      onSuccess: (response, restart) =>
        onDone(
          response,
          t(restart ? "nodes.config.resetRestarting" : "nodes.config.resetDone")
        ),
      onError: (e) => {
        generateErrorMessage(e, toast);
      },
    }
  );
  const restart = useMutation(() => nodeConfigApi.restart(nodeId), {
    onSuccess: () => {
      queryClient.invalidateQueries(FetchNodesQueryKey);
      toast({
        title: t("nodes.config.restartScheduled"),
        status: "success",
        isClosable: true,
        position: "top",
        duration: 3000,
      });
      // The leader records the applied template once the restart went through.
      setTimeout(() => {
        queryClient.invalidateQueries(queryKey);
        queryClient.invalidateQueries(FetchNodesQueryKey);
      }, 4000);
    },
    onError: (e) => {
        generateErrorMessage(e, toast);
      },
  });

  const isBusy = save.isLoading || reset.isLoading || restart.isLoading;
  const blockingError = parsed.error || serverError;
  // An untouched shared config may still be saved: that pins the node to it.
  const canSave = !!data && !blockingError && !isValidating && (isDirty || !data.custom);
  const isDisabledNode = node.status === "disabled";

  if (isLoading || !data)
    return (
      <ModalBody>
        <Flex h="300px" alignItems="center" justifyContent="center">
          <Spinner />
        </Flex>
      </ModalBody>
    );

  return (
    <>
      <ModalBody>
        <HStack mb={3} flexWrap="wrap" gap={2}>
          <Text fontWeight="semibold" fontSize="md" mr={1}>
            {node.name}
            <Text as="span" fontWeight="normal" fontSize="sm" opacity={0.6}>
              {" · "}
              {node.address}
            </Text>
          </Text>
          <Badge
            colorScheme={data.custom ? "purple" : "gray"}
            textTransform="none"
            px={2}
            py={0.5}
          >
            {t(data.custom ? "nodes.config.modeOwn" : "nodes.config.modeShared")}
          </Badge>
          {data.pending && (
            <Badge colorScheme="yellow" textTransform="none" px={2} py={0.5}>
              {t("nodes.config.pending")}
            </Badge>
          )}
          {isDirty && (
            <Badge colorScheme="blue" textTransform="none" px={2} py={0.5}>
              {t("nodes.config.unsaved")}
            </Badge>
          )}
          {data.custom && data.updated_at && (
            <Text fontSize="xs" opacity={0.7}>
              {t("nodes.config.updated", {
                date: dayjs(data.updated_at + "Z").format("DD.MM.YYYY HH:mm"),
                by: data.updated_by || "—",
              })}
            </Text>
          )}
        </HStack>

        <Text fontSize="sm" opacity={0.8} mb={3}>
          {t(data.custom ? "nodes.config.hintOwn" : "nodes.config.hintShared")}
        </Text>

        {data.pending && !isDirty && (
          <Alert
            status="warning"
            fontSize="sm"
            borderRadius="md"
            mb={3}
            py={2}
            flexWrap="wrap"
            rowGap={2}
          >
            <AlertIcon />
            <AlertDescription flex="1" minW="200px">
              {t("nodes.config.pendingHint")}
            </AlertDescription>
            <Button
              size="xs"
              ml={3}
              flexShrink={0}
              leftIcon={<RestartIcon />}
              onClick={() => restart.mutate()}
              isLoading={restart.isLoading}
              isDisabled={isBusy || isDisabledNode}
            >
              {t("nodes.config.restartNode")}
            </Button>
          </Alert>
        )}

        <Flex gap={4} direction={{ base: "column", xl: "row" }}>
          <Box flex="1" minW={0}>
            <HStack justifyContent="space-between" mb={2} flexWrap="wrap" gap={2}>
              <ButtonGroup size="xs" isAttached variant="outline">
                <Button
                  onClick={() => setView("editor")}
                  isActive={view === "editor"}
                >
                  {t("nodes.config.viewEditor")}
                </Button>
                <Button
                  onClick={() => setView("diff")}
                  isActive={view === "diff"}
                  isDisabled={!!parsed.error}
                >
                  {t("nodes.config.viewDiff")}
                </Button>
              </ButtonGroup>
              <HStack>
                <Tooltip label={t("nodes.config.copySharedHint")} placement="top">
                  <Button
                    size="xs"
                    variant="ghost"
                    leftIcon={<CopyIcon />}
                    onClick={() => {
                      load(data.shared_config);
                      setView("editor");
                    }}
                    isDisabled={sameAsShared}
                  >
                    {t("nodes.config.copyShared")}
                  </Button>
                </Tooltip>
                <Button
                  size="xs"
                  variant="ghost"
                  onClick={() => load(parsed.value)}
                  isDisabled={!!parsed.error}
                >
                  {t("nodes.config.format")}
                </Button>
              </HStack>
            </HStack>

            <Box
              h={{ base: "360px", xl: "520px" }}
              display={view === "editor" ? "block" : "none"}
            >
              <JsonEditor json={editorJson} onChange={setText} />
            </Box>
            {view === "diff" && (
              <Box
                h={{ base: "360px", xl: "520px" }}
                border="1px solid"
                borderColor="gray.300"
                _dark={{ borderColor: "gray.500" }}
                borderRadius={5}
                overflow="hidden"
              >
                <DiffView
                  shared={pretty(data.shared_config)}
                  current={parsed.error ? text : pretty(parsed.value)}
                />
              </Box>
            )}
            {parsed.error && (
              <Text fontSize="xs" color="red.500" mt={1}>
                {t("nodes.config.jsonError", { error: parsed.error })}
              </Text>
            )}
          </Box>

          <Box
            w={{ base: "full", xl: "280px" }}
            flexShrink={0}
            maxH={{ xl: "552px" }}
            overflowY="auto"
          >
            <SummaryPanel
              summary={parsed.error ? null : summary}
              error={parsed.error ? null : serverError}
              isValidating={isValidating}
            />
          </Box>
        </Flex>
      </ModalBody>

      <ModalFooter>
        <Flex
          w="full"
          justifyContent="space-between"
          flexWrap="wrap"
          gap={2}
        >
          <Menu placement="top-start">
            <MenuButton
              as={Button}
              size="sm"
              variant="ghost"
              colorScheme="red"
              leftIcon={<ResetIcon />}
              rightIcon={<ChevronIcon />}
              isDisabled={!data.custom || isBusy}
              isLoading={reset.isLoading}
            >
              {t("nodes.config.reset")}
            </MenuButton>
            <MenuList fontSize="sm">
              <MenuItem onClick={() => reset.mutate(false)}>
                {t("nodes.config.resetOnly")}
              </MenuItem>
              <MenuItem
                onClick={() => reset.mutate(true)}
                isDisabled={isDisabledNode}
              >
                {t("nodes.config.resetAndRestart")}
              </MenuItem>
            </MenuList>
          </Menu>

          <HStack>
            <Tooltip label={t("nodes.config.saveHint")} placement="top">
              <Button
                size="sm"
                variant="outline"
                onClick={() => save.mutate(false)}
                isDisabled={!canSave || isBusy}
                isLoading={save.isLoading && save.variables === false}
              >
                {t("nodes.config.save")}
              </Button>
            </Tooltip>
            <Tooltip label={t("nodes.config.saveRestartHint")} placement="top">
              <Button
                size="sm"
                colorScheme="primary"
                leftIcon={<RestartIcon />}
                onClick={() => save.mutate(true)}
                isDisabled={!canSave || isBusy || isDisabledNode}
                isLoading={save.isLoading && save.variables === true}
              >
                {t("nodes.config.saveAndRestart")}
              </Button>
            </Tooltip>
          </HStack>
        </Flex>
      </ModalFooter>
    </>
  );
};
