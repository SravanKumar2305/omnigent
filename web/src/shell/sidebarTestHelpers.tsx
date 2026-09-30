/** Standard mounted Sidebar with a fresh query cache for each test. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import { Sidebar } from "./Sidebar";

export function renderSidebar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SidebarDataProvider>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/"]}>
            <Sidebar open={true} onClose={vi.fn()} />
          </MemoryRouter>
        </TooltipProvider>
      </SidebarDataProvider>
    </QueryClientProvider>,
  );
}
