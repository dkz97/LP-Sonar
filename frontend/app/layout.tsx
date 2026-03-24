import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "LP-Sonar",
  description: "DeFi LP monitoring system",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
