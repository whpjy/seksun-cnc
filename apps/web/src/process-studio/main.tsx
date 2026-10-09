import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import Login from "./Login";
import "../styles.css";
import "./studio.css";

createRoot(document.getElementById("root")!).render(<StrictMode><Login /></StrictMode>);
