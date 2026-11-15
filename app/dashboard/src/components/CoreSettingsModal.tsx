import {
  Badge,
  Box,
  Button,
  chakra,
  CircularProgress,
  Flex,
  Link,
  Spinner,
  FormControl,
  FormLabel,
  HStack,
  IconButton,
  Modal,
  ModalBody,
  ModalCloseButton,
  ModalContent,
  ModalFooter,
  ModalHeader,
  ModalOverlay,
  Select,
  Text,
  Tooltip,
  useToast,
  useColorMode
} from "@chakra-ui/react";
import {
  ArrowPathIcon,
  ArrowsPointingInIcon,
  ArrowsPointingOutIcon,
  Cog6ToothIcon,
} from "@heroicons/react/24/outline";
import { joinPaths } from "@remix-run/router";
import classNames from "classnames";
import { CoreConfigTarget, useCoreSettings } from "contexts/CoreSettingsContext";
import { useDashboard } from "contexts/DashboardContext";
import debounce from "lodash.debounce";
import { FC, Fragment, useCallback, useEffect, useRef, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { useMutation } from "react-query";
import { ReadyState } from "react-use-websocket";
import { useWebSocket } from "react-use-websocket/dist/lib/use-websocket";
import { getAuthToken } from "utils/authStorage";
import { getBaseApi } from "utils/runtimeConfig";
import { Icon } from "./Icon";
import { JsonEditor } from "./JsonEditor";
import "./JsonEditor/themes.js";
import { useNodesQuery } from "contexts/NodesContext";
import { CoreConfigNav } from "./CoreConfigNav";
import { NodeConfigEditor } from "./NodeConfigEditor";

export const MAX_NUMBER_OF_LOGS = 500;

const UsageIcon = chakra(Cog6ToothIcon, {
  baseStyle: {
    w: 5,
    h: 5,
  },
});
export const ReloadIcon = chakra(ArrowPathIcon, {
  baseStyle: {
    w: 4,
    h: 4,
  },
});

export const FullScreenIcon = chakra(ArrowsPointingOutIcon, {
  baseStyle: {
    w: 4,
    h: 4,
  },
});
export const ExitFullScreenIcon = chakra(ArrowsPointingInIcon, {
  baseStyle: {
    w: 3,
    h: 3,
  },
});

const getStatus = (status: string) => {
  return {
    [ReadyState.CONNECTING]: "connecting",
    [ReadyState.OPEN]: "connected",
    [ReadyState.CLOSING]: "closed",
    [ReadyState.CLOSED]: "closed",
    [ReadyState.UNINSTANTIATED]: "closed",
  }[status];
};

const getWebsocketUrl = (nodeID: string) => {
  try {
    const baseApi = getBaseApi();
    let baseURL = new URL(
      baseApi.startsWith("/")
        ? window.location.origin + baseApi
        : baseApi
    );

    return (
      (baseURL.protocol === "https:" ? "wss://" : "ws://") +
      joinPaths([
        baseURL.host + baseURL.pathname,
        !nodeID ? "/core/logs" : `/node/${nodeID}/logs`,
      ]) +
      "?interval=1&token=" +
      getAuthToken()
    );
  } catch (e) {
    console.error("Unable to generate websocket url");
    console.error(e);
    return null;
  }
};

let logsTmp: string[] = [];
const CoreSettingModalContent: FC<{
  onSelectNode: (id: number) => void;
}> = ({ onSelectNode }) => {

  const { colorMode } = useColorMode();

  const { data: nodes } = useNodesQuery();
  const disabled = false;
  const [selectedNode, setNode] = useState<string>("");

  const handleLog = (id: string, title: string) => {
    if (id === selectedNode) return;
    else if (id === "host") {
      setNode("");
      setLogs([]);
    } else {
      setNode(id);
      setLogs([]);
    }
  };

  const { isEditingCore } = useDashboard();
  const {
    fetchCoreSettings,
    updateConfig,
    isLoading,
    config,
    isPostLoading,
    version,
    restartCore,
  } = useCoreSettings();
  const logsDiv = useRef<HTMLDivElement | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const { t } = useTranslation();
  const toast = useToast();
  const form = useForm({
    defaultValues: { config: config || {} },
  });

  useEffect(() => {
    if (config) form.setValue("config", config);
  }, [config]);

  useEffect(() => {
    if (isEditingCore) fetchCoreSettings();
  }, [isEditingCore]);
  "".startsWith;
  const scrollShouldStayOnEnd = useRef(true);
  const updateLogs = useCallback(
    debounce((logs: string[]) => {
      const isScrollOnEnd =
        Math.abs(
          (logsDiv.current?.scrollTop || 0) -
            (logsDiv.current?.scrollHeight || 0) +
            (logsDiv.current?.offsetHeight || 0)
        ) < 10;
      if (logsDiv.current && isScrollOnEnd)
        scrollShouldStayOnEnd.current = true;
      else scrollShouldStayOnEnd.current = false;
      if (logs.length < 40) setLogs(logs);
    }, 300),
    []
  );

  const { readyState } = useWebSocket(getWebsocketUrl(selectedNode), {
    onMessage: (e: any) => {
      logsTmp.push(e.data);
      if (logsTmp.length > MAX_NUMBER_OF_LOGS)
        logsTmp = logsTmp.splice(0, logsTmp.length - MAX_NUMBER_OF_LOGS);
      updateLogs([...logsTmp]);
    },
    shouldReconnect: () => true,
    reconnectAttempts: 10,
    reconnectInterval: 1000,
  });

  useEffect(() => {
    if (logsDiv.current && scrollShouldStayOnEnd.current)
      logsDiv.current.scrollTop = logsDiv.current?.scrollHeight;
  }, [logs]);

  useEffect(() => {
    return () => {
      logsTmp = [];
    };
  }, []);

  const status = getStatus(readyState.toString());

  const { mutate: handleRestartCore, isLoading: isRestarting } =
    useMutation(restartCore);

  const handleOnSave = ({ config }: any) => {
    updateConfig(config)
      .then(() => {
        toast({
          title: t("core.successMessage"),
          status: "success",
          isClosable: true,
          position: "top",
          duration: 3000,
        });
      })
      .catch((e) => {
        let message = t("core.generalErrorMessage");
        if (typeof e.response._data.detail === "object")
          message =
            e.response._data.detail[Object.keys(e.response._data.detail)[0]];
        if (typeof e.response._data.detail === "string")
          message = e.response._data.detail;

        toast({
          title: message,
          status: "error",
          isClosable: true,
          position: "top",
          duration: 3000,
        });
      });
  };
  const editorRef = useRef<HTMLDivElement>(null);
  const [isFullScreen, setFullScreen] = useState(false);
  const handleFullScreen = () => {
    if (document.fullscreenElement) {
      document.exitFullscreen();
      setFullScreen(false);
    } else {
      editorRef.current?.requestFullscreen();
      setFullScreen(true);
    }
  };
  return (
    <form onSubmit={form.handleSubmit(handleOnSave)}>
      <ModalBody>
        <FormControl>
          <HStack justifyContent="space-between" alignItems="flex-start">
            <FormLabel>
              {t("core.configuration")}{" "}
              {isLoading && <CircularProgress isIndeterminate size="15px" />}
            </FormLabel>
            <HStack gap={0}>
              <Tooltip label="Xray Version" placement="top">
                <Badge height="100%" textTransform="lowercase">
                  {version && `v${version}`}
                </Badge>
              </Tooltip>
            </HStack>
          </HStack>
          {nodes && nodes.length > 0 && (
            <Text fontSize="xs" opacity={0.75} mb={2}>
              {(() => {
                const shared = nodes.filter((n) => !n.has_custom_config);
                const own = nodes.filter((n) => n.has_custom_config);
                return (
                  <>
                    {shared.length
                      ? t("nodes.config.sharedUsedBy", {
                          nodes: shared.map((n) => n.name).join(", "),
                        })
                      : t("nodes.config.sharedUsedByNone")}
                    {own.length > 0 && (
                      <>
                        {" "}
                        {t("nodes.config.sharedOwnNote")}{" "}
                        {own.map((n, i) => (
                          <Fragment key={n.id}>
                            {i > 0 && ", "}
                            <Link
                              color="primary.500"
                              _dark={{ color: "primary.200" }}
                              onClick={() => onSelectNode(n.id as number)}
                            >
                              {n.name}
                            </Link>
                          </Fragment>
                        ))}
                      </>
                    )}
                  </>
                );
              })()}
            </Text>
          )}
          <Box position="relative" ref={editorRef} minHeight="300px">
            <Controller
              control={form.control}
              name="config"
              render={({ field }) => (
                <JsonEditor json={config} onChange={field.onChange} />
              )}
            />
            <IconButton
              size="xs"
              aria-label="full screen"
              variant="ghost"
              position="absolute"
              top="2"
              right="4"
              onClick={handleFullScreen}
            >
              {!isFullScreen ? <FullScreenIcon /> : <ExitFullScreenIcon />}
            </IconButton>
          </Box>
        </FormControl>
        <FormControl mt="4">
          <HStack
            justifyContent="space-between"
            style={{ paddingBottom: "1rem" }}
          >
            <HStack>
              {nodes?.[0] && (
                <Select
                  size="sm"
                  style={{ width: "auto" }}
                  disabled={disabled}
                  bg={disabled ? "gray.100" : "transparent"}
                  _dark={{
                    bg: disabled ? "gray.600" : "transparent",
                  }}
                  sx={{
                    option: {
                      backgroundColor: colorMode === "dark" ? "#222C3B" : "white"
                    }
                  }}
                  onChange={(v) =>
                    handleLog(
                      v.currentTarget.value,
                      v.currentTarget.selectedOptions[0].text
                    )
                  }
                >
                  <option key={"host"} value={"host"} defaultChecked>
                    Master
                  </option>
                  {nodes &&
                    nodes.map((s) => {
                      return (
                        <option key={s.address} value={String(s.id)}>
                          {t(s.name)}
                        </option>
                      );
                    })}
                </Select>
              )}
              <FormLabel className="w-au">{t("core.logs")}</FormLabel>
            </HStack>
            <Text as={FormLabel}>{t(`core.socket.${status}`)}</Text>
          </HStack>
          <Box
            border="1px solid"
            borderColor="gray.300"
            bg="#F9F9F9"
            _dark={{
              borderColor: "gray.500",
              bg: "#2e3440",
            }}
            borderRadius={5}
            minHeight="200px"
            maxHeight={"250px"}
            p={2}
            overflowY="auto"
            ref={logsDiv}
          >
            {logs.map((message, i) => (
              <Text fontSize="xs" opacity={0.8} key={i} whiteSpace="pre-line">
                {message}
              </Text>
            ))}
          </Box>
        </FormControl>
      </ModalBody>
      <ModalFooter>
        <HStack w="full" justifyContent="space-between">
          <HStack>
            <Box>
              <Button
                size="sm"
                leftIcon={
                  <ReloadIcon
                    className={classNames({
                      "animate-spin": isRestarting,
                    })}
                  />
                }
                onClick={() => handleRestartCore()}
              >
                {t(isRestarting ? "core.restarting" : "core.restartCore")}
              </Button>
            </Box>
          </HStack>

          <HStack>
            <Button
              size="sm"
              variant="solid"
              colorScheme="primary"
              px="5"
              type="submit"
              isDisabled={isLoading || isPostLoading}
              isLoading={isPostLoading}
            >
              {t("core.save")}
            </Button>
          </HStack>
        </HStack>
      </ModalFooter>
    </form>
  );
};
export const CoreSettingsModal: FC = () => {
  const { isEditingCore } = useDashboard();
  const { target, setTarget } = useCoreSettings();
  const { data: nodes } = useNodesQuery();
  const { t } = useTranslation();
  // Unsaved edits of the node editor; the shared editor never had a guard.
  const dirty = useRef(false);

  const leave = () => {
    if (dirty.current && !window.confirm(t("nodes.config.discardConfirm")))
      return false;
    dirty.current = false;
    return true;
  };
  const select = (next: CoreConfigTarget) => {
    if (next === target || !leave()) return;
    setTarget(next);
  };
  const onClose = () => {
    if (!leave()) return;
    useDashboard.setState({ isEditingCore: false });
    setTarget("shared");
  };

  const node =
    target === "shared" ? undefined : (nodes || []).find((n) => n.id === target);

  return (
    <Modal
      isOpen={isEditingCore}
      onClose={onClose}
      size="6xl"
      closeOnOverlayClick={false}
    >
      <ModalOverlay bg="blackAlpha.300" backdropFilter="blur(10px)" />
      <ModalContent mx="3" w="full">
        <ModalHeader pt={6}>
          <HStack gap={2}>
            <Icon color="primary">
              <UsageIcon color="white" />
            </Icon>
            <Text fontWeight="semibold" fontSize="lg">
              {t("core.title")}
            </Text>
          </HStack>
        </ModalHeader>
        <ModalCloseButton mt={3} />
        <Flex direction={{ base: "column", lg: "row" }} alignItems="stretch">
          <CoreConfigNav nodes={nodes || []} target={target} onSelect={select} />
          <Box flex="1" minW={0}>
            {target === "shared" ? (
              <CoreSettingModalContent onSelectNode={select} />
            ) : node ? (
              <NodeConfigEditor
                key={node.id}
                node={node}
                onDirtyChange={(value) => (dirty.current = value)}
              />
            ) : (
              <Flex h="300px" alignItems="center" justifyContent="center">
                <Spinner />
              </Flex>
            )}
          </Box>
        </Flex>
      </ModalContent>
    </Modal>
  );
};
