import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Modal } from "@/components/ui/Modal";

function renderModal(props: Partial<ComponentProps<typeof Modal>> = {}) {
  const onClose = vi.fn();
  render(
    <Modal open onClose={onClose} title="Dialog" {...props}>
      <p>body</p>
    </Modal>,
  );
  return onClose;
}

describe("Modal", () => {
  it("renders a close button and dismisses through it by default", async () => {
    const onClose = renderModal();
    await userEvent.click(screen.getByRole("button", { name: "Close dialog" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("dismisses on Escape by default", async () => {
    const onClose = renderModal();
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("closes on backdrop click when closeOnBackdrop is left on", async () => {
    const onClose = renderModal();
    await userEvent.click(screen.getByRole("presentation"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("ignores backdrop clicks when closeOnBackdrop is off", async () => {
    const onClose = renderModal({ closeOnBackdrop: false });
    await userEvent.click(screen.getByRole("presentation"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("hides the close button and ignores Escape when not dismissible", async () => {
    const onClose = renderModal({ dismissible: false });
    expect(screen.queryByRole("button", { name: "Close dialog" })).not.toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();
  });
});
