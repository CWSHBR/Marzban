import { Badge, HStack, Text, Tooltip } from "@chakra-ui/react";
import { useHAStatus } from "contexts/HAContext";
import { FC } from "react";
import { useTranslation } from "react-i18next";

export const HABadge: FC = () => {
  const { data: status } = useHAStatus();
  const { t } = useTranslation();

  if (!status || !status.self.ha_enabled) return null;

  const isLeader = status.self.role === "leader";

  return (
    <Tooltip label={t("ha.title")} placement="bottom">
      <Badge
        colorScheme={isLeader ? "green" : "gray"}
        rounded="full"
        display="inline-flex"
        alignItems="center"
        px={2}
        py={1}
        maxW="150px"
      >
        <HStack gap={1} css={{ direction: "ltr" }}>
          <Text
            fontSize="0.7rem"
            fontWeight="semibold"
            letterSpacing="tighter"
            textTransform="uppercase"
          >
            {t(isLeader ? "ha.leader" : "ha.follower")}
          </Text>
          <Text
            fontSize="0.7rem"
            fontWeight="normal"
            letterSpacing="tighter"
            opacity={0.8}
            isTruncated
          >
            {status.self.panel_id}
          </Text>
        </HStack>
      </Badge>
    </Tooltip>
  );
};
