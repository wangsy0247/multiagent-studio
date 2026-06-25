/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // API 代理 — 开发环境将 /api/* 转发到 App 服务
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://localhost:8000/api/:path*",
      },
    ];
  },
  images: {
    domains: ["localhost"],
  },
};

module.exports = nextConfig;
