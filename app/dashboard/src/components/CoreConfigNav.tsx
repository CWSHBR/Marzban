import {
  Box,
  Button,
  chakra,
  HStack,
  Menu,
  MenuButton,
  MenuItem,
  MenuList,
  Select,
  Text,
  Tooltip,
  VStack,
} from "@chakra-ui/react";
import {
  GlobeAltIcon,
  PlusIcon as HeroPlusIcon,
  ServerIcon,
} from "@heroicons/react/24/outline";
import { CoreConfigTarget } from "contexts/CoreSettingsContext";
import { NodeType } from "contexts/NodesContext";
import { FC, ReactNode } from "react";
import { useTranslation } from "react-i18next";

const iconStyle = { baseStyle: { w: 4, h: 4, flexShrink: 0 } };
const SharedIcon = chakra(GlobeAltIcon, iconStyle);
const NodeIcon = chakra(ServerIcon, iconStyle);
const PlusIcon = chakra(HeroPlusIcon, iconStyle);

const NavItem: FC<{
  isActive: boolean;
  onClick: () => void;
  icon: ReactNode;
  title: string;
  subtitle?: string;
  dot?: { color: string; label: string } | null;
}> = ({ isActive, onClick, icon, title, subtitle, dot }) => (
  <Button
    onClick={onClick}
    variant="ghost"
    size="sm"
    h="auto"
    py={2}
    px={2}
    w="full"
    justifyContent="flex-start"
    fontWeight="normal"
    borderRadius="md"
    bg={isActive ? "primary.50" : undefined}
    color={isActive ? "primary.700" : undefined}
    _hover={{ bg: isActive ? "primary.50" : "gray.100" }}
    _dark={{
      bg: isActive ? "whiteAlpha.200" : undefined,
      color: isActive ? "white" : undefined,
      _hover: { bg: isActive ? "whiteAlpha.200" : "whiteAlpha.100" },
    }}
    aria-current={isActive ? "page" : undefined}
  >
    <HStack w="full" spacing={2} alignItems="center" minW={0}>
      {icon}
      <Box flex="1" minW={0} textAlign="left">
        <Text fontSize="sm" fontWeight={isActive ? "semibold" : "medium"} noOfLines={1}>
          {title}
        </Text>
        {subtitle && (
          <Text fontSize="xs" opacity={0.65} noOfLines={1}>
            {subtitle}
          </Text>
        )}
      </Box>
      {dot && (
        <Tooltip label={dot.label} placement="top">
          <Box w={2} h={2} borderRadius="full" bg={dot.color} flexShrink={0} />
        </Tooltip>
      )}
    </HStack>
  </Button>
);

/**
 * Left column of the core settings dialog: the shared config, then every
 * node that carries its own, then a way to give one more node its own.
 */
export const CoreConfigNav: FC<{
  nodes: NodeType[];
  target: CoreConfigTarget;
  onSelect: (target: CoreConfigTarget) => void;
}> = ({ nodes, target, onSelect }) => {
  const { t } = useTranslation();

  const own = nodes.filter((n) => n.has_custom_config);
  // A node picked from "add" stays listed while it is still a draft.
  const draft = nodes.find((n) => n.id === target && !n.has_custom_config);
  const listed = draft ? [...own, draft] : own;
  const onShared = nodes.filter((n) => !n.has_custom_config);
  const addable = onShared.filter((n) => n.id !== target);

  const dotFor = (node: NodeType) =>
    node.config_pending
      ? { color: "yellow.400", label: t("nodes.config.pendingHint") }
      : null;

  return (
    <>
      {/* Phones: one select carries all of it. */}
      <Box display={{ base: "block", lg: "none" }} px={6} pb={3}>
        <Select
          size="sm"
          value={String(target)}
          onChange={(e) => {
            const value = e.currentTarget.value;
            onSelect(value === "shared" ? "shared" : Number(value));
          }}
        >
          <option value="shared">{t("nodes.config.navShared")}</option>
          {listed.length > 0 && (
            <optgroup label={t("nodes.config.navOwnTitle")}>
              {listed.map((node) => (
                <option key={node.id} value={String(node.id)}>
                  {node.name}
                  {node.config_pending ? ` · ${t("nodes.config.pendingBadge")}` : ""}
                  {!node.has_custom_config ? ` · ${t("nodes.config.navDraft")}` : ""}
                </option>
              ))}
            </optgroup>
          )}
          {addable.length > 0 && (
            <optgroup label={t("nodes.config.navAddGroup")}>
              {addable.map((node) => (
                <option key={node.id} value={String(node.id)}>
                  + {node.name}
                </option>
              ))}
            </optgroup>
          )}
        </Select>
      </Box>

      <VStack
        display={{ base: "none", lg: "flex" }}
        w="230px"
        flexShrink={0}
        alignItems="stretch"
        spacing={1}
        pl={4}
        pr={3}
        pb={6}
        borderRight="1px solid"
        borderColor="gray.200"
        _dark={{ borderColor: "gray.600" }}
      >
        <NavItem
          isActive={target === "shared"}
          onClick={() => onSelect("shared")}
          icon={<SharedIcon />}
          title={t("nodes.config.navShared")}
          subtitle={t("nodes.config.navSharedUsedBy", { count: onShared.length })}
        />

        <Text
          fontSize="xs"
          fontWeight="semibold"
          textTransform="uppercase"
          letterSpacing="wider"
          opacity={0.55}
          pt={4}
          pb={1}
          px={2}
        >
          {t("nodes.config.navOwnTitle")}
        </Text>

        {listed.length === 0 && (
          <Text fontSize="xs" opacity={0.65} px={2} pb={1}>
            {t("nodes.config.navEmpty")}
          </Text>
        )}

        {listed.map((node) => (
          <NavItem
            key={node.id}
            isActive={target === node.id}
            onClick={() => onSelect(node.id as number)}
            icon={<NodeIcon />}
            title={node.name}
            subtitle={
              node.has_custom_config ? node.address : t("nodes.config.navDraft")
            }
            dot={dotFor(node)}
          />
        ))}

        <Menu placement="bottom-start" isLazy>
          <MenuButton
            as={Button}
            size="sm"
            variant="ghost"
            colorScheme="primary"
            leftIcon={<PlusIcon />}
            justifyContent="flex-start"
            // MenuButton wraps the label in a flex-grow span; keep it by the icon
            textAlign="left"
            fontWeight="medium"
            px={2}
            mt={1}
            isDisabled={addable.length === 0}
          >
            {t("nodes.config.navAdd")}
          </MenuButton>
          <MenuList fontSize="sm" maxH="320px" overflowY="auto">
            <Text px={3} py={1} fontSize="xs" opacity={0.65}>
              {t("nodes.config.navAddHint")}
            </Text>
            {addable.map((node) => (
              <MenuItem
                key={node.id}
                icon={<NodeIcon />}
                onClick={() => onSelect(node.id as number)}
              >
                {node.name}
                <Text as="span" opacity={0.6}>
                  {" · "}
                  {node.address}
                </Text>
              </MenuItem>
            ))}
          </MenuList>
        </Menu>
        {addable.length === 0 && nodes.length > 0 && (
          <Text fontSize="xs" opacity={0.6} px={2}>
            {t("nodes.config.navAddNone")}
          </Text>
        )}
      </VStack>
    </>
  );
};
