"use client";

import { useMe } from "@/lib/hooks";
import { AppShell } from "@/components/AppShell";

export default function AppLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  const { data: me, isLoading, isError, error } = useMe();

  // Transient failure (network blip / expired session) - render the shell
  // anyway; Clerk's proxy redirects unauthenticated page loads to sign-in.
  if (isError && !(error instanceof Error && error.message === "Network request failed")) {
    return <AppShell>{children}</AppShell>;
  }

  return (
    <AppShell fullName={me?.full_name ?? me?.email ?? null} isAdmin={me?.role === "admin"} loading={isLoading}>
      {children}
    </AppShell>
  );
}
