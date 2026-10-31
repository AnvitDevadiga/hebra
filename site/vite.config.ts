/// <reference types="vitest" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  base: "/hebra/",
  plugins: [react()],
  test: { environment: "jsdom", environmentOptions: { jsdom: { url: "http://localhost/hebra/" } } },
});
