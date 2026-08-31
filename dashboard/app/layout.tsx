import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "GOLDM strangle engine",
  description: "Read-only monitoring for the MCX GOLDM short-strangle engine.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      {/*
        Browser extensions mutate <body> before React hydrates — Grammarly adds
        `data-gr-ext-installed` and `data-new-gr-c-s-check-loaded`, and password
        managers do similar. React then reports a hydration mismatch for
        attributes this app never rendered and cannot control.

        Scoped deliberately to this one element: `suppressHydrationWarning` is
        not inherited by descendants, so a real mismatch anywhere inside the
        page is still reported. Silencing it here removes noise that would
        otherwise train the reader to ignore the warning that matters.
      */}
      <body suppressHydrationWarning>{children}</body>
    </html>
  );
}
