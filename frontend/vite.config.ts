import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In dev, /plan and /health are proxied to the FastAPI server.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/plan": "http://localhost:8000",
      "/book": "http://localhost:8000", // also covers /booking/*
      "/health": "http://localhost:8000",
    },
  },
});
