import { useCallback, useEffect, useState } from "react";
import { adminCreateUser, adminListUsers } from "../api";
import type { AdminUser } from "../types";

interface Props {
  onImpersonate: (userId: string) => Promise<void> | void;
}

export function Admin({ onImpersonate }: Props) {
  // ---- Users ----
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [usersError, setUsersError] = useState<string | null>(null);
  const [loadingUsers, setLoadingUsers] = useState(false);
  const [newUserEmail, setNewUserEmail] = useState("");
  const [newUserName, setNewUserName] = useState("");
  const [newUserPassword, setNewUserPassword] = useState("");
  const [newUserRole, setNewUserRole] = useState<"user" | "admin">("user");
  const [newUserSubmitting, setNewUserSubmitting] = useState(false);
  const [newUserMsg, setNewUserMsg] = useState<string | null>(null);

  const refreshUsers = useCallback(async () => {
    setLoadingUsers(true);
    setUsersError(null);
    try {
      setUsers(await adminListUsers());
    } catch (err) {
      setUsersError(err instanceof Error ? err.message : "Failed");
    } finally {
      setLoadingUsers(false);
    }
  }, []);

  const submitNewUser = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (newUserSubmitting) return;
      setNewUserSubmitting(true);
      setNewUserMsg(null);
      try {
        const u = await adminCreateUser({
          email: newUserEmail.trim(),
          password: newUserPassword,
          display_name: newUserName.trim() || undefined,
          role: newUserRole,
        });
        setNewUserMsg(`Created ${u.email}`);
        setNewUserEmail("");
        setNewUserName("");
        setNewUserPassword("");
        setNewUserRole("user");
        await refreshUsers();
      } catch (err) {
        setNewUserMsg(err instanceof Error ? err.message : "Failed");
      } finally {
        setNewUserSubmitting(false);
      }
    },
    [
      newUserEmail,
      newUserPassword,
      newUserName,
      newUserRole,
      newUserSubmitting,
      refreshUsers,
    ],
  );

  useEffect(() => {
    refreshUsers();
  }, [refreshUsers]);

  return (
    <main className="admin">
      <header className="admin__header">
        <h1>Admin</h1>
        <p className="admin__subtitle">
          User management, roles, and impersonation
        </p>
      </header>

      <section className="admin__section">
        <div className="admin__section-head">
          <h2>Users</h2>
          <button
            className="btn btn--ghost btn--sm"
            onClick={refreshUsers}
            disabled={loadingUsers}
          >
            ↻ Refresh
          </button>
        </div>

        {usersError && <div className="admin__error">{usersError}</div>}

        <table className="admin__table">
          <thead>
            <tr>
              <th>Email</th>
              <th>Name</th>
              <th>Role</th>
              <th>Created</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id}>
                <td className="mono">{u.email}</td>
                <td>{u.display_name || "—"}</td>
                <td>
                  <span className={`role-chip role-chip--${u.role}`}>
                    {u.role}
                  </span>
                </td>
                <td className="muted">
                  {new Date(u.created_at).toLocaleDateString()}
                </td>
                <td>
                  <button
                    className="btn btn--ghost btn--sm"
                    onClick={() => onImpersonate(u.id)}
                  >
                    Login as
                  </button>
                </td>
              </tr>
            ))}
            {!loadingUsers && users.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
                  No users yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>

        <form className="admin__form" onSubmit={submitNewUser}>
          <h3>Create test user</h3>
          <div className="admin__form-row">
            <label>
              <span>Email</span>
              <input
                type="email"
                required
                value={newUserEmail}
                onChange={(e) => setNewUserEmail(e.target.value)}
                disabled={newUserSubmitting}
              />
            </label>
            <label>
              <span>Display name</span>
              <input
                type="text"
                value={newUserName}
                onChange={(e) => setNewUserName(e.target.value)}
                disabled={newUserSubmitting}
              />
            </label>
            <label>
              <span>Password (min 8)</span>
              <input
                type="text"
                required
                minLength={8}
                value={newUserPassword}
                onChange={(e) => setNewUserPassword(e.target.value)}
                disabled={newUserSubmitting}
              />
            </label>
            <label>
              <span>Role</span>
              <select
                value={newUserRole}
                onChange={(e) =>
                  setNewUserRole(e.target.value as "user" | "admin")
                }
                disabled={newUserSubmitting}
              >
                <option value="user">user</option>
                <option value="admin">admin</option>
              </select>
            </label>
            <button
              type="submit"
              className="btn btn--primary"
              disabled={newUserSubmitting}
            >
              {newUserSubmitting ? "…" : "Create"}
            </button>
          </div>
          {newUserMsg && <div className="admin__hint">{newUserMsg}</div>}
        </form>
      </section>
    </main>
  );
}
