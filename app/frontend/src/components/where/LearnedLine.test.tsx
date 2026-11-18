import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { site } from "../../state/searchFixtures";
import { createSearchStore, SearchStoreProvider, type SearchApi } from "../../state/searchStore";
import { render } from "../../test-utils";
import { ToastProvider } from "../toast/ToastProvider";
import SearchScreen from "../search/SearchScreen";

// What was learned of a board the owner added (docs/PRD.md 37.17, item G) is one line the server puts after the board's
// own status words. It shows wherever the status shows (the row and the open pane's Status), in no place of its own.
// The host is made up.

const LINE = "Read by a data request; fills title; place unsupported; paced at one request every 29 seconds (terms).";
const FAILED = "The data request stage failed: was refused (403).";

function setup(status: string) {
  const sites = [site("a", { url: "https://board.example.test/jobs", label: "Board", status })];
  const api = {
    profile: vi.fn(async () => ({})),
    sites: vi.fn(async () => ({ sites, job_systems: [], watch_in_play: false, terms: [] })),
    siteRoles: vi.fn(async () => ({ total: 0, items: [] })),
    jobSystemRoles: vi.fn(async () => ({ total: 0, items: [] })),
  } as unknown as SearchApi;
  const store = createSearchStore({ api, openLive: (_e, onState) => (onState("open"), { close: () => {} }), resiteMs: 0 });
  function Screen() {
    const [s] = useState(store);
    return (
      <ToastProvider>
        <SearchStoreProvider store={s}>
          <SearchScreen />
        </SearchStoreProvider>
      </ToastProvider>
    );
  }
  return Screen;
}

async function list() {
  const region = await screen.findByRole("region", { name: "Where to look" });
  await waitFor(() => expect(within(region).getByText("Sites to search")).toBeInTheDocument());
  return region;
}

describe("the line of what was learned for a board", () => {
  it("shows in the board's existing status, after the status words, and in no row or panel of its own", async () => {
    const Screen = setup(`Read nightly at 12:30 AM. ${LINE}`);
    render(<Screen />, { hash: "#/profile/where" });
    const region = await list();
    expect(within(region).getAllByText(`Read nightly at 12:30 AM. ${LINE}`).length).toBeGreaterThan(0);
    expect(within(region).getAllByRole("listitem").filter((li) => li.textContent?.includes("Read by a data request"))).toHaveLength(1);
  });

  it("shows in the open pane's Status fact, and says the stage that failed and why", async () => {
    const Screen = setup(`Starts tonight at 1:00 AM. ${FAILED}`);
    render(<Screen />, { hash: "#/profile/where" });
    const region = await list();
    fireEvent.click(within(region).getByRole("button", { name: "Board" }));
    const status = await within(region).findAllByText(/stage failed/);
    expect(status.length).toBeGreaterThanOrEqual(1);
    expect(status.every((el) => el.textContent?.includes("was refused (403)"))).toBe(true);
  });
});
