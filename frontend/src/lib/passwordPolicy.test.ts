import { describe, expect, it } from "vitest";
import { PASSWORD_MIN_LENGTH, passwordProblems, passwordProblemText } from "@/lib/passwordPolicy";

describe("passwordProblems", () => {
  it("accepts a password that satisfies the server policy", () => {
    expect(passwordProblems("Str0ng!Passphrase")).toEqual([]);
  });

  it("requires the configured minimum length", () => {
    expect(PASSWORD_MIN_LENGTH).toBe(10);
    expect(passwordProblems("Ab1!")).toContain(`at least ${PASSWORD_MIN_LENGTH} characters`);
  });

  it("requires a digit, a letter and a symbol, matching the backend rule set", () => {
    expect(passwordProblems("abcdefghijklmnop")).toContain("a digit");
    expect(passwordProblems("abcdefghijklmnop")).toContain("a symbol");
    expect(passwordProblems("1234567890123!")).toContain("a letter");
  });

  it("reports nothing for an empty field so the form is not red on first paint", () => {
    expect(passwordProblems("")).toEqual([]);
  });

  it("renders a single readable sentence", () => {
    expect(passwordProblemText("abcdef")).toMatch(/^Password needs /);
    expect(passwordProblemText("Str0ng!Passphrase")).toBeUndefined();
  });
});