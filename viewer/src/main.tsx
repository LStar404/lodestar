import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { applyAppearance } from "./lib/appearance";
import "./styles.css";
import "./workspace.css";

applyAppearance();   // 外观设置先行应用，避免首帧闪默认背景

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
