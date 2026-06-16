import type { Metadata } from "next";
import Link from "next/link";
import { AuthGate } from "@/components/auth-gate";
import { IdentityMenu } from "@/components/identity-menu";
import { AuthProvider } from "@/lib/auth";
import "./globals.css";

export const metadata: Metadata = {
  title: "Red Team Dashboard",
  description: "Red team operations — engagements, agents, findings, reporting",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // `dark` is pinned on <html>: the app is always the monochrome dark theme.
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-background font-sans text-foreground antialiased">
        <AuthProvider>
          <header className="sticky top-0 z-40 border-b border-border bg-background/80 backdrop-blur">
            <div className="container flex h-14 items-center justify-between">
              <Link href="/" className="group flex items-center gap-2.5">
                {/* The lone accent in the chrome — a single ember mark. */}
                <span className="h-3.5 w-1 rounded-full bg-critical" />
                <span className="text-sm font-semibold tracking-tight">
                  Red Team Dashboard
                </span>
              </Link>
              <IdentityMenu />
            </div>
          </header>
          <AuthGate>
            <main className="container py-8">{children}</main>
          </AuthGate>
        </AuthProvider>
      </body>
    </html>
  );
}
