import {
  Alert,
  AlertDialog,
  AlertDialogBody,
  AlertDialogContent,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogOverlay,
  AlertIcon,
  Badge,
  Box,
  Button,
  chakra,
  HStack,
  Modal,
  ModalBody,
  ModalCloseButton,
  ModalContent,
  ModalHeader,
  ModalOverlay,
  SimpleGrid,
  Spinner,
  Text,
  Tooltip,
  useToast,
  VStack,
} from "@chakra-ui/react";
import { ServerStackIcon } from "@heroicons/react/24/outline";
import {
  HAMemberType,
  HAOutboxType,
  HAStatusType,
  isMemberStale,
  requestStepdown,
  useHAStatus,
} from "contexts/HAContext";
import useGetUser from "hooks/useGetUser";
import {
  FC,
  PropsWithChildren,
  ReactNode,
  useEffect,
  useRef,
  useState,
} from "react";
import { Trans, useTranslation } from "react-i18next";
import { useMutation } from "react-query";
import {
  generateErrorMessage,
  generateSuccessMessage,
} from "utils/toastHandler";
import { useDashboard } from "../contexts/DashboardContext";
import { Icon } from "./Icon";

const ModalIcon = chakra(ServerStackIcon, {
  baseStyle: {
    w: 5,
    h: 5,
  },
});

// A queue that is merely non-empty is normal; one that is not draining is not.
const QUEUE_WARN_AGE = 30;

const formatDuration = (seconds: number): string => {
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m ${total % 60}s`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
};

/**
 * The lease value only refreshes when the status query does (every 5s), which
 * makes an expiry that is a handful of seconds away look frozen. Tick it down
 * locally and re-seed it from the server on every refetch.
 */
const useLeaseCountdown = (
  expiresIn: number | null | undefined,
  updatedAt: number
): number | null => {
  const [remaining, setRemaining] = useState<number | null>(null);

  useEffect(() => {
    setRemaining(typeof expiresIn === "number" ? expiresIn : null);
  }, [expiresIn, updatedAt]);

  useEffect(() => {
    const timer = setInterval(() => {
      setRemaining((value) => (value === null ? null : Math.max(0, value - 1)));
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  return remaining;
};

type CardProps = PropsWithChildren<{ accent?: string }>;

const Card: FC<CardProps> = ({ accent, children }) => (
  <VStack
    w="full"
    align="stretch"
    gap={1}
    p={3}
    border="1px solid"
    borderRadius="4px"
    _dark={{ borderColor: accent || "gray.600" }}
    _light={{ borderColor: accent || "gray.200" }}
  >
    {children}
  </VStack>
);

const SectionTitle: FC<PropsWithChildren> = ({ children }) => (
  <Text
    fontSize="xs"
    fontWeight="semibold"
    textTransform="uppercase"
    letterSpacing="wide"
    opacity={0.7}
  >
    {children}
  </Text>
);

const StatRow: FC<{ label: string; children: ReactNode }> = ({
  label,
  children,
}) => (
  <HStack w="full" justifyContent="space-between" gap={3}>
    <Text fontSize="sm" opacity={0.7}>
      {label}
    </Text>
    <Box fontSize="sm" fontWeight="medium" textAlign="end">
      {children}
    </Box>
  </HStack>
);

const RoleBadge: FC<{ role: HAMemberType["role"]; isStale?: boolean }> = ({
  role,
  isStale = false,
}) => {
  const { t } = useTranslation();
  return (
    <Badge
      colorScheme={isStale ? "red" : role === "leader" ? "green" : "gray"}
      rounded="full"
      px={2}
      fontSize="0.7rem"
    >
      {t(role === "leader" ? "ha.leader" : "ha.follower")}
    </Badge>
  );
};

const MemberCard: FC<{ member: HAMemberType }> = ({ member }) => {
  const { t } = useTranslation();
  const isStale = isMemberStale(member);

  return (
    <Card
      accent={isStale ? "red.400" : member.is_self ? "primary.400" : undefined}
    >
      <HStack w="full" justifyContent="space-between" gap={2}>
        <HStack minW={0} gap={2}>
          <Text fontSize="sm" fontWeight="semibold" isTruncated>
            {member.panel_id}
          </Text>
          {member.is_self && (
            <Badge colorScheme="blue" rounded="full" px={2} fontSize="0.65rem">
              {t("ha.thisPanel")}
            </Badge>
          )}
        </HStack>
        <RoleBadge role={member.role} isStale={isStale} />
      </HStack>
      <HStack w="full" justifyContent="space-between" gap={2}>
        <Text fontSize="xs" opacity={0.7}>
          {member.version ? `v${member.version}` : t("ha.unknown")}
        </Text>
        <Text
          fontSize="xs"
          opacity={isStale ? 1 : 0.7}
          color={isStale ? "red.400" : undefined}
        >
          {typeof member.last_seen_age === "number"
            ? t("ha.seenAgo", { age: formatDuration(member.last_seen_age) })
            : t("ha.neverSeen")}
        </Text>
      </HStack>
    </Card>
  );
};

const LeaseSection: FC<{ status: HAStatusType; updatedAt: number }> = ({
  status,
  updatedAt,
}) => {
  const { t } = useTranslation();
  const expiresIn = useLeaseCountdown(status.lease?.expires_in, updatedAt);

  return (
    <Card>
      {!status.lease || !status.lease.holder ? (
        <Text fontSize="sm" opacity={0.7}>
          {t("ha.noLease")}
        </Text>
      ) : (
        <>
          <StatRow label={t("ha.leaseHolder")}>{status.lease.holder}</StatRow>
          <StatRow label={t("ha.epoch")}>{status.lease.epoch}</StatRow>
          <StatRow label={t("ha.leaseExpiresIn")}>
            <Text
              as="span"
              color={expiresIn !== null && expiresIn <= 5 ? "red.400" : undefined}
            >
              {expiresIn === null ? t("ha.unknown") : formatDuration(expiresIn)}
            </Text>
          </StatRow>
          {typeof status.self.leader_for_seconds === "number" && (
            <StatRow label={t("ha.leaderFor")}>
              {formatDuration(status.self.leader_for_seconds)}
            </StatRow>
          )}
          {typeof status.self.fencing_seconds_left === "number" && (
            <StatRow label={t("ha.fencingLeft")}>
              {formatDuration(status.self.fencing_seconds_left)}
            </StatRow>
          )}
        </>
      )}
    </Card>
  );
};

const QueueCard: FC<{
  title: string;
  description?: string;
  queue: HAOutboxType;
}> = ({ title, description, queue }) => {
  const { t } = useTranslation();
  const { pending, oldest_age_seconds: oldestAge } = queue;
  const isBacklogged =
    typeof pending === "number" &&
    pending > 0 &&
    typeof oldestAge === "number" &&
    oldestAge > QUEUE_WARN_AGE;
  const tone = queue.backlog_bailout ? "red" : isBacklogged ? "yellow" : "gray";

  return (
    <Card accent={tone === "gray" ? undefined : `${tone}.400`}>
      <HStack w="full" justifyContent="space-between" gap={2}>
        <Text fontSize="sm" fontWeight="medium">
          {title}
        </Text>
        <Badge colorScheme={tone} rounded="full" px={2} fontSize="0.7rem">
          {typeof pending === "number" ? pending : t("ha.unknown")}
        </Badge>
      </HStack>
      <StatRow label={t("ha.queueOldest")}>
        {typeof oldestAge === "number"
          ? formatDuration(oldestAge)
          : t("ha.unknown")}
      </StatRow>
      {queue.backlog_bailout && (
        <Text fontSize="xs" color="red.400">
          {t("ha.queueBailout")}
        </Text>
      )}
      {description && (
        <Text fontSize="xs" opacity={0.7}>
          {description}
        </Text>
      )}
    </Card>
  );
};

const NodesSection: FC<{ nodes: HAStatusType["nodes"] }> = ({ nodes }) => {
  const { t } = useTranslation();
  const entries = Object.entries(nodes.by_status);

  return (
    <Card accent={nodes.needs_restart > 0 ? "yellow.400" : undefined}>
      <HStack w="full" gap={2} flexWrap="wrap">
        {entries.length === 0 ? (
          <Text fontSize="sm" opacity={0.7}>
            {t("ha.noNodes")}
          </Text>
        ) : (
          entries.map(([nodeStatus, count]) => (
            <Badge
              key={nodeStatus}
              colorScheme={
                nodeStatus === "connected"
                  ? "green"
                  : nodeStatus === "error"
                  ? "red"
                  : "gray"
              }
              rounded="full"
              px={2}
              fontSize="0.7rem"
            >
              {t(`nodeModal.status.${nodeStatus}`)}: {count}
            </Badge>
          ))
        )}
      </HStack>
      {nodes.needs_restart > 0 && (
        <Text fontSize="xs" color="yellow.500" fontWeight="medium">
          {t("ha.needsRestart", { nodes: nodes.needs_restart })}
        </Text>
      )}
    </Card>
  );
};

const HandoverSection: FC<{ status: HAStatusType }> = ({ status }) => {
  const { t } = useTranslation();
  const toast = useToast();
  const [isConfirming, setIsConfirming] = useState(false);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const isInProgress = !!status.lease?.stepdown_requested;

  const { isLoading, mutate } = useMutation(requestStepdown, {
    onSuccess: (result) => {
      generateSuccessMessage(
        t("ha.handoverRequested", { target: result.target || "" }),
        toast
      );
      setIsConfirming(false);
    },
    onError: (e) => {
      generateErrorMessage(e, toast);
      setIsConfirming(false);
    },
  });

  return (
    <>
      <Tooltip
        label={status.handover_blocked_reason}
        placement="top"
        isDisabled={status.can_handover || !status.handover_blocked_reason}
      >
        <Box w="full">
          <Button
            w="full"
            size="sm"
            colorScheme="primary"
            isDisabled={!status.can_handover || isInProgress || isLoading}
            isLoading={isLoading}
            leftIcon={isInProgress ? <Spinner size="xs" /> : undefined}
            onClick={() => setIsConfirming(true)}
          >
            {t(isInProgress ? "ha.handoverInProgress" : "ha.handover")}
          </Button>
        </Box>
      </Tooltip>

      <AlertDialog
        isOpen={isConfirming}
        leastDestructiveRef={cancelRef}
        onClose={() => setIsConfirming(false)}
        isCentered
        size="sm"
      >
        <AlertDialogOverlay bg="blackAlpha.300" backdropFilter="blur(10px)">
          <AlertDialogContent mx="3">
            <AlertDialogHeader fontSize="lg" fontWeight="semibold" pt={6}>
              {t("ha.handoverConfirm.title")}
            </AlertDialogHeader>
            <AlertDialogBody
              fontSize="sm"
              _dark={{ color: "gray.400" }}
              color="gray.600"
            >
              <Trans components={{ b: <b /> }}>
                {t("ha.handoverConfirm.prompt", {
                  target: status.handover_target || "",
                })}
              </Trans>
            </AlertDialogBody>
            <AlertDialogFooter display="flex">
              <Button
                ref={cancelRef}
                size="sm"
                mr={3}
                w="full"
                variant="outline"
                onClick={() => setIsConfirming(false)}
              >
                {t("cancel")}
              </Button>
              <Button
                size="sm"
                w="full"
                colorScheme="primary"
                isLoading={isLoading}
                onClick={() => mutate()}
              >
                {t("ha.handoverConfirm.confirm")}
              </Button>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialogOverlay>
      </AlertDialog>
    </>
  );
};

const HAModalContent: FC = () => {
  const { t } = useTranslation();
  const { data: status, dataUpdatedAt, isLoading } = useHAStatus();
  const { userData, getUserIsSuccess, getUserIsPending } = useGetUser();

  const isSudo = () => {
    if (!getUserIsPending && getUserIsSuccess) {
      return userData.is_sudo;
    }
    return false;
  };

  if (isLoading || !status)
    return (
      <Text fontSize="sm" opacity={0.7}>
        {t("hostsDialog.loading")}
      </Text>
    );

  if (!status.self.ha_enabled)
    return (
      <Alert status="info">
        <AlertIcon />
        {t("ha.notEnabled")}
      </Alert>
    );

  return (
    <VStack w="full" align="stretch" gap={5}>
      {status.self.last_error && (
        <Alert status="error">
          <AlertIcon />
          {status.self.last_error}
        </Alert>
      )}

      <VStack w="full" align="stretch" gap={2}>
        <SectionTitle>{t("ha.panels")}</SectionTitle>
        <SimpleGrid columns={{ base: 1, md: 2 }} spacing={2} w="full">
          {status.members.map((member) => (
            <MemberCard key={member.panel_id} member={member} />
          ))}
        </SimpleGrid>
      </VStack>

      <VStack w="full" align="stretch" gap={2}>
        <SectionTitle>{t("ha.lease")}</SectionTitle>
        <LeaseSection status={status} updatedAt={dataUpdatedAt} />
        {isSudo() && <HandoverSection status={status} />}
      </VStack>

      <VStack w="full" align="stretch" gap={2}>
        <SectionTitle>{t("ha.queues")}</SectionTitle>
        <SimpleGrid columns={{ base: 1, md: 2 }} spacing={2} w="full">
          <QueueCard
            title={t("ha.syncQueue")}
            description={t("ha.syncQueue.desc")}
            queue={status.outbox}
          />
          <QueueCard
            title={t("ha.notificationsQueue")}
            queue={status.notifications}
          />
        </SimpleGrid>
      </VStack>

      <VStack w="full" align="stretch" gap={2}>
        <SectionTitle>{t("ha.nodes")}</SectionTitle>
        <NodesSection nodes={status.nodes} />
      </VStack>
    </VStack>
  );
};

export const HAModal: FC = () => {
  const { isShowingHA, onShowingHA } = useDashboard();
  const { t } = useTranslation();
  const onClose = () => onShowingHA(false);

  return (
    <Modal isOpen={isShowingHA} onClose={onClose} size="2xl">
      <ModalOverlay bg="blackAlpha.300" backdropFilter="blur(10px)" />
      <ModalContent mx="3" w="full">
        <ModalHeader pt={6}>
          <HStack gap={2}>
            <Icon color="primary">
              <ModalIcon color="white" />
            </Icon>
            <Text fontWeight="semibold" fontSize="lg">
              {t("ha.title")}
            </Text>
          </HStack>
        </ModalHeader>
        <ModalCloseButton mt={3} />
        <ModalBody pb={6} pt={3}>
          <HAModalContent />
        </ModalBody>
      </ModalContent>
    </Modal>
  );
};
