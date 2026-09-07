import type { IconName } from "@/components/ui/Icon";
import type { Permission, User } from "@/lib/types";
import { can, isAdmin } from "@/stores/auth";

export interface NavItem {
  to: string;
  label: string;
  icon: IconName;
  group: "operate" | "govern";
  // Permission-gated items follow the RBAC matrix; role-gated items guard routes
  // backed by /admin/*, which the server restricts to the admin role.
  permission?: Permission;
  adminOnly?: boolean;
}

export const NAV_ITEMS: NavItem[] = [
  { to: "/dashboard", label: "Dashboard", icon: "grid", permission: "metrics:read", group: "operate" },
  { to: "/campaigns", label: "Campaigns", icon: "megaphone", permission: "campaign:read", group: "operate" },
  { to: "/runs", label: "Optimization Runs", icon: "activity", permission: "run:read", group: "operate" },
  { to: "/actions", label: "Action Approvals", icon: "checkSquare", permission: "run:read", group: "operate" },
  { to: "/alerts", label: "Alerts", icon: "bell", permission: "alert:read", group: "operate" },
  { to: "/creatives", label: "Creatives", icon: "sparkles", permission: "campaign:read", group: "operate" },
  { to: "/experiments", label: "Experiments", icon: "flask", group: "govern", adminOnly: true },
  { to: "/audit", label: "Audit Log", icon: "scroll", group: "govern", adminOnly: true },
  { to: "/settings", label: "System", icon: "sliders", group: "govern", adminOnly: true },
];

export function isNavAllowed(item: NavItem, user: User | null): boolean {
  if (item.adminOnly && !isAdmin(user)) return false;
  if (item.permission && !can(user, item.permission)) return false;
  return true;
}

export function visibleNav(user: User | null): { operate: NavItem[]; govern: NavItem[] } {
  const allowed = NAV_ITEMS.filter((item) => isNavAllowed(item, user));
  return {
    operate: allowed.filter((item) => item.group === "operate"),
    govern: allowed.filter((item) => item.group === "govern"),
  };
}

export const GROUP_LABELS = { operate: "Operate", govern: "Governance" } as const;