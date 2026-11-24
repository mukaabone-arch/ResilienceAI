import type { NextConfig } from "next";

// The browser talks to /api/v1 on the same origin; Next proxies it to the FastAPI service.
const API_URL = process.env.API_URL ?? "http://localhost:8000";

const config: NextConfig = {
  async rewrites() {
    return [{ source: "/api/v1/:path*", destination: `${API_URL}/api/v1/:path*` }];
  },
};

export default config;
