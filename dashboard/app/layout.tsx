import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "GOLDM strangle engine",
  description: "Read-only monitoring for the MCX GOLDM short-strangle engine.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
