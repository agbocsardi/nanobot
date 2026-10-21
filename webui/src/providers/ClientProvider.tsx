import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  type ReactNode,
} from "react";

import type { NanobotClient } from "@/lib/nanobot-client";
import type { WebUIIngressLimits } from "@/lib/types";

interface ClientContextValue {
  client: NanobotClient;
  token: string;
  getToken: () => string;
  modelName: string | null;
  ingressLimits: WebUIIngressLimits | null;
}

const ClientContext = createContext<ClientContextValue | null>(null);

export function ClientProvider({
  client,
  token,
  modelName = null,
  ingressLimits = null,
  children,
}: {
  client: NanobotClient;
  token: string;
  modelName?: string | null;
  ingressLimits?: WebUIIngressLimits | null;
  children: ReactNode;
}) {
  const tokenRef = useRef(token);
  tokenRef.current = token;
  const getToken = useCallback(() => tokenRef.current, []);

  // Resuming a backgrounded page is the moment a dead socket has to be caught:
  // the browser may keep reporting it OPEN after the peer is gone, so nothing
  // else would ever trigger the reconnect that recovers a missed turn.
  useEffect(() => {
    if (typeof document === "undefined") return;
    const revive = () => {
      if (document.visibilityState === "hidden") return;
      // Hosts may supply a partial client double; probing is best-effort.
      if (typeof client.probeLiveness === "function") client.probeLiveness();
    };
    document.addEventListener("visibilitychange", revive);
    window.addEventListener("online", revive);
    return () => {
      document.removeEventListener("visibilitychange", revive);
      window.removeEventListener("online", revive);
    };
  }, [client]);

  const value = useMemo(
    () => ({ client, token, getToken, modelName, ingressLimits }),
    [client, getToken, ingressLimits, modelName, token],
  );

  return (
    <ClientContext.Provider value={value}>
      {children}
    </ClientContext.Provider>
  );
}

export function useClient(): ClientContextValue {
  const ctx = useContext(ClientContext);
  if (!ctx) {
    throw new Error("useClient must be used within a ClientProvider");
  }
  return ctx;
}
