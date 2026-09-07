import { describe, expect, it } from "vitest";
import { can, selectPermissions } from "@/stores/auth";
import type { Permission, Role, User } from "@/lib/types";

function userFor(role: Role): User {
  return {
    id: `u-${role}`,
    email: `${role}@example.com`,
    full_name: role,
    role,
    is_active: true,
    must_change_password: false,
    last_login_at: null,
    created_at: null,
  };
}

// This matrix is a contract test against _ROLE_PERMISSIONS in
// backend/src/adoptimizer/core/security.py. If either side changes, the test
// fails here rather than the operator getting a 403 mid-task.
const BACKEND_MATRIX: Record<Role, Permission[] | "*"> = {
  admin: "*",
  optimizer: [
    "campaign:read",
    "campaign:write",
    "run:read",
    "run:trigger",
    "action:approve",
    "action:execute",
    "alert:read",
    "alert:ack",
    "creative:write",
    "metrics:read",
    "system:read",
  ],
  analyst: ["campaign:read", "run:read", "alert:read", "alert:ack", "metrics:read", "system:read"],
  viewer: ["campaign:read", "run:read", "alert:read", "metrics:read"],
};

describe("selectPermissions", () => {
  it("matches the server-side RBAC matrix for every role", () => {
    for (const [role, expected] of Object.entries(BACKEND_MATRIX) as Array<[Role, Permission[] | "*"]>) {
      const actual = selectPermissions(userFor(role));
      if (expected === "*") {
        expect(actual).toBe("*");
        continue;
      }
      expect([...(actual as Permission[])].sort()).toEqual([...expected].sort());
    }
  });

  it("grants nothing to an unauthenticated caller", () => {
    expect(selectPermissions(null)).toEqual([]);
  });
});

describe("can", () => {
  it("treats admin as unrestricted", () => {
    expect(can(userFor("admin"), "audit:read")).toBe(true);
    expect(can(userFor("admin"), "user:manage")).toBe(true);
  });

  it("lets an optimizer approve and execute but not manage users", () => {
    expect(can(userFor("optimizer"), "action:approve")).toBe(true);
    expect(can(userFor("optimizer"), "action:execute")).toBe(true);
    expect(can(userFor("optimizer"), "user:manage")).toBe(false);
  });

  it("keeps an analyst read-only with respect to money", () => {
    expect(can(userFor("analyst"), "metrics:read")).toBe(true);
    expect(can(userFor("analyst"), "action:execute")).toBe(false);
    expect(can(userFor("analyst"), "campaign:write")).toBe(false);
  });

  it("denies a viewer every mutating capability", () => {
    expect(can(userFor("viewer"), "run:trigger")).toBe(false);
    expect(can(userFor("viewer"), "alert:ack")).toBe(false);
    expect(can(userFor("viewer"), "campaign:read")).toBe(true);
  });

  it("denies everything when signed out", () => {
    expect(can(null, "campaign:read")).toBe(false);
  });
});