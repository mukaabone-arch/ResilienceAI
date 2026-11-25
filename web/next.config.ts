import type { NextConfig } from "next";

// The browser talks to /api/v1 on the same origin; Next proxies it to the FastAPI service.
const API_URL = process.env.API_URL ?? "http://localhost:8000";

const config: NextConfig = {
  devIndicators: false,
  experimental: {
    // Evidence uploads (up to 25 MB) pass through the /api/v1 rewrite; Next buffers proxied
    // bodies and cuts them at 10 MB by default.
    proxyClientMaxBodySize: "30mb",
  },
  async rewrites() {
    return [{ source: "/api/v1/:path*", destination: `${API_URL}/api/v1/:path*` }];
  },
};

export default config;
