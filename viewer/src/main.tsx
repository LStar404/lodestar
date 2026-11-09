import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { applyAppearance } from "./lib/appearance";
import { migrateBrandStorage } from "./lib/brand";
import "./styles.css";
import "./workspace.css";

migrateBrandStorage();  // 旧 note-gen-* 键一次性迁到 lodestar-*，用户偏好不丢
applyAppearance();      // 外观设置先行应用，避免首帧闪默认背景

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
