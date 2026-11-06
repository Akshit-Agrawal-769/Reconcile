import { useEffect, useState } from "react";
import { api, getToken, setToken, setUnauthorizedHandler, type User } from "./api";
import { roleLabel } from "./format";
import { navigate, useRoute, type Route } from "./router";
import { Login } from "./pages/Login";
import { Board } from "./pages/Board";
import { Period } from "./pages/Period";
import { ClientView } from "./pages/ClientView";
import { Followups } from "./pages/Followups";
import { Reports } from "./pages/Reports";

export function App() {
  const route = useRoute();
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(Boolean(getToken()));

  useEffect(() => {
    setUnauthorizedHandler(() => { setUser(null); navigate("#/login"); });
    if (!getToken()) return;
    api.me().then(setUser).catch(() => setToken(null)).finally(() => setChecking(false));
  }, []);

  if (checking) return <main className="page"><p className="muted">Loading…</p></main>;
  if (!user) {
    return <Login onLogin={(u) => { setUser(u); navigate("#/"); }} />;
  }

  function logout() {
    setToken(null);
    setUser(null);
    navigate("#/login");
  }

  return (
    <>
      <header className="bar" role="banner">
        <a className="wordmark" href={user.role === "client" ? "#/" : "#/board"}>Reconcile</a>
        <span className="who">{roleLabel(user.role)}</span>
        <button className="link" onClick={logout}>Log out</button>
      </header>
      <main className="page">
        <Screen route={route} user={user} />
      </main>
    </>
  );
}

function Screen({ route, user }: { route: Route; user: User }) {
  switch (route.name) {
    case "board":
      return <Board user={user} />;
    case "period":
      return <Period key={route.periodId} periodId={route.periodId} user={user} />;
    case "reports":
      return <Reports key={route.clientId} clientId={route.clientId} />;
    case "followups":
      return <Followups key={route.periodId} periodId={route.periodId} />;
    case "client":
      return <ClientView key={route.periodId} periodId={route.periodId} />;
    default:
      return <Home user={user} />;
  }
}

/** "#/" and unknown routes: staff land on the board, a client on their latest month. */
function Home({ user }: { user: User }) {
  const [message, setMessage] = useState("Loading…");
  useEffect(() => {
    if (user.role !== "client") { navigate("#/board"); return; }
    (async () => {
      const clients = await api.clients();
      if (!clients.length) { setMessage("No client is linked to this login."); return; }
      const periods = await api.clientPeriods(clients[0].client_id);
      if (!periods.length) { setMessage("Your CA has not opened a month yet."); return; }
      navigate(`#/client/${periods[0].period_id}`);
    })().catch((e) => setMessage(String(e.message ?? e)));
  }, [user]);
  return <p className="muted">{message}</p>;
}
