import type { Metadata } from "next";
import "@fontsource-variable/public-sans";
import "./globals.css";
import { AppFrame } from "@/components/Shell";

export const metadata: Metadata = {
  title: "ResilienceAI — Test coordination",
  description: "Plan, coordinate and govern resilience tests.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <AppFrame>{children}</AppFrame>
      </body>
    </html>
  );
}
