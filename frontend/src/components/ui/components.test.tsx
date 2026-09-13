import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { StatusPill } from "@/components/ui/StatusPill";
import { Pagination } from "@/components/ui/Pagination";
import { Table, type Column } from "@/components/ui/Table";
import { EmptyState } from "@/components/ui/EmptyState";
import { BarList } from "@/components/charts/BarList";
import { Donut } from "@/components/charts/Donut";
import { Sparkline } from "@/components/charts/Sparkline";

describe("Button", () => {
  it("fires the click handler", async () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Approve</Button>);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("is disabled while loading so a slow mutation cannot be double-submitted", async () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick} loading>Approve</Button>);
    const button = screen.getByRole("button", { name: "Approve" });
    expect(button).toBeDisabled();
    await userEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });

  it("defaults to type=button so it never submits an enclosing form by accident", () => {
    render(<Button>Save</Button>);
    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute("type", "button");
  });
});

describe("Badge and StatusPill", () => {
  it("renders the supplied tone class", () => {
    render(<Badge tone="positive">Active</Badge>);
    expect(screen.getByText("Active").className).toContain("text-pos");
  });

  it("maps each domain value onto a stable tone", () => {
    render(
      <>
        <StatusPill domain="run" value="succeeded" />
        <StatusPill domain="run" value="failed" />
        <StatusPill domain="action" value="proposed" />
        <StatusPill domain="action" value="suppressed" />
        <StatusPill domain="severity" value="critical" />
      </>,
    );
    expect(screen.getByText("Succeeded").className).toContain("text-pos");
    expect(screen.getByText("Failed").className).toContain("text-neg");
    expect(screen.getByText("Proposed").className).toContain("text-brand-300");
    // Withheld proposals are a first-class status, so they need their own tone
    // rather than falling through to the neutral unknown-value default.
    expect(screen.getByText("Suppressed").className).toContain("text-warn");
    expect(screen.getByText("Critical").className).toContain("text-neg");
  });

  it("falls back to a neutral tone for an unknown value instead of throwing", () => {
    render(<StatusPill domain="campaign" value="something_new" />);
    expect(screen.getByText("Something New")).toBeInTheDocument();
  });
});

describe("Pagination", () => {
  it("reports the visible slice of the result set", () => {
    render(<Pagination page={2} pageSize={25} total={80} onChange={() => undefined} />);
    expect(screen.getByText(/26–50 of 80/)).toBeInTheDocument();
    expect(screen.getByText("2 / 4")).toBeInTheDocument();
  });

  it("disables the previous button on the first page", () => {
    render(<Pagination page={1} pageSize={25} total={80} onChange={() => undefined} />);
    expect(screen.getByLabelText("Previous page")).toBeDisabled();
    expect(screen.getByLabelText("Next page")).toBeEnabled();
  });

  it("emits the requested page", async () => {
    const onChange = vi.fn();
    render(<Pagination page={1} pageSize={25} total={80} onChange={onChange} />);
    await userEvent.click(screen.getByLabelText("Next page"));
    expect(onChange).toHaveBeenCalledWith(2);
  });

  it("shows an empty range without dividing by zero", () => {
    render(<Pagination page={1} pageSize={25} total={0} onChange={() => undefined} />);
    expect(screen.getByText(/0–0 of 0/)).toBeInTheDocument();
  });
});

interface Row {
  id: string;
  name: string;
}

const columns: Array<Column<Row>> = [
  { key: "id", header: "ID", cell: (row) => row.id },
  { key: "name", header: "Name", cell: (row) => row.name },
];

describe("Table", () => {
  it("renders headers and rows", () => {
    render(
      <Table columns={columns} rows={[{ id: "1", name: "Alpha" }]} rowKey={(row) => row.id} />,
    );
    expect(screen.getByText("Name")).toBeInTheDocument();
    expect(screen.getByText("Alpha")).toBeInTheDocument();
  });

  it("shows the empty state when there are no rows", () => {
    render(
      <Table columns={columns} rows={[]} rowKey={(row) => row.id} emptyTitle="Nothing to show" />,
    );
    expect(screen.getByText("Nothing to show")).toBeInTheDocument();
  });

  it("prefers an error notice over rows so a failure is never shown as empty", () => {
    render(
      <Table
        columns={columns}
        rows={[{ id: "1", name: "Alpha" }]}
        rowKey={(row) => row.id}
        error="boom"
      />,
    );
    expect(screen.getByText("Could not load data")).toBeInTheDocument();
    expect(screen.queryByText("Alpha")).not.toBeInTheDocument();
  });

  it("invokes the row click handler", async () => {
    const onRowClick = vi.fn();
    render(
      <Table
        columns={columns}
        rows={[{ id: "1", name: "Alpha" }]}
        rowKey={(row) => row.id}
        onRowClick={onRowClick}
      />,
    );
    await userEvent.click(screen.getByText("Alpha"));
    expect(onRowClick).toHaveBeenCalledWith({ id: "1", name: "Alpha" });
  });
});

describe("EmptyState", () => {
  it("renders title, hint and action", () => {
    render(
      <EmptyState title="No campaigns" hint="Create one" action={<button type="button">New</button>} />,
    );
    expect(screen.getByText("No campaigns")).toBeInTheDocument();
    expect(screen.getByText("Create one")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New" })).toBeInTheDocument();
  });
});

describe("BarList", () => {
  it("scales bars against the largest value", () => {
    render(
      <BarList
        items={[
          { id: "a", label: "Alpha", value: 4 },
          { id: "b", label: "Beta", value: 2 },
        ]}
        format={(value) => `${value}x`}
      />,
    );
    expect(screen.getByText("4x")).toBeInTheDocument();
    expect(screen.getByText("2x")).toBeInTheDocument();
  });

  it("survives an all-zero series without dividing by zero", () => {
    render(<BarList items={[{ id: "a", label: "Alpha", value: 0 }]} />);
    expect(screen.getByText("Alpha")).toBeInTheDocument();
  });

  it("shows the empty label when there is nothing to rank", () => {
    render(<BarList items={[]} emptyLabel="Nothing ranked" />);
    expect(screen.getByText("Nothing ranked")).toBeInTheDocument();
  });
});

describe("Donut", () => {
  it("renders one arc per slice and a legend entry", () => {
    render(
      <Donut
        slices={[
          { id: "a", label: "Critical", value: 3, color: "#f00" },
          { id: "b", label: "Warning", value: 1, color: "#ff0" },
        ]}
      />,
    );
    expect(screen.getByText("Critical")).toBeInTheDocument();
    expect(screen.getByText("75.0%")).toBeInTheDocument();
    expect(screen.getByText("25.0%")).toBeInTheDocument();
  });

  it("reports no data instead of drawing an empty ring", () => {
    render(<Donut slices={[{ id: "a", label: "Critical", value: 0, color: "#f00" }]} />);
    expect(screen.getByText("No data")).toBeInTheDocument();
  });
});

describe("Sparkline", () => {
  it("draws a path when there are at least two points", () => {
    const { container } = render(<Sparkline values={[1, 4, 2, 6]} />);
    expect(container.querySelectorAll("path").length).toBeGreaterThan(0);
  });

  it("degrades to a placeholder block for insufficient data", () => {
    const { container } = render(<Sparkline values={[3]} />);
    expect(container.querySelector("svg")).toBeNull();
  });
});