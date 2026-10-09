import { useState, type FormEvent } from "react";
import { Box, Eye, EyeOff, LogIn } from "lucide-react";
import Studio from "./Studio";
import "./login.css";

const LOGIN_KEY = "nexus.process-studio.login.v1";

export default function Login() {
  const [authenticated, setAuthenticated] = useState(() => sessionStorage.getItem(LOGIN_KEY) === "admin");
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [visible, setVisible] = useState(false);
  const [error, setError] = useState("");
  function submit(event: FormEvent) {
    event.preventDefault();
    if (username !== "admin" || password !== "Admin@123") {
      setError("用户名或密码错误");
      return;
    }
    sessionStorage.setItem(LOGIN_KEY, "admin");
    setPassword("");
    setAuthenticated(true);
  }
  function logout() {
    sessionStorage.removeItem(LOGIN_KEY);
    setUsername("admin");
    setPassword("");
    setVisible(false);
    setError("");
    setAuthenticated(false);
  }
  if (authenticated) return <Studio onLogout={logout} />;
  return <main className="login-shell process-login">
    <div className="login-visual" aria-hidden="true"><img alt="" src="/process-studio/login-cad-compare-bg-v2.png" /><span className="login-scanline" /></div>
    <section className="login-intro" aria-labelledby="loginIntroTitle">
      <p className="login-intro-kicker">NEXUS · AI PROCESS DESIGN STUDIO</p>
      <h1 id="loginIntroTitle">智能工艺设计平台</h1>
      <p className="login-intro-copy">上传 STEP / STP 三维模型，智能体将识别制造特征、规划加工工序、计算刀路并验证加工结果，形成可复核的工艺方案。</p>
    </section>
    <section className="login-panel" aria-labelledby="loginTitle">
      <header className="login-brand"><span aria-hidden="true"><Box size={27} strokeWidth={1.7} /></span><div><h2 id="loginTitle">智能工艺设计平台</h2><p>AI PROCESS DESIGN STUDIO</p></div></header>
      <form className="login-form" onSubmit={submit}>
        <label><span>用户名</span><input autoComplete="username" required autoFocus value={username} onChange={event => { setUsername(event.target.value); setError(""); }} /></label>
        <label><span>密码</span><div className="password-field"><input autoComplete="current-password" required type={visible ? "text" : "password"} value={password} onChange={event => { setPassword(event.target.value); setError(""); }} /><button type="button" title={visible ? "隐藏密码" : "显示密码"} aria-pressed={visible} onClick={() => setVisible(!visible)}>{visible ? <EyeOff /> : <Eye />}</button></div></label>
        {error && <div className="login-error" role="alert">{error}</div>}
        <button className="login-submit" type="submit"><LogIn /><span>登录</span></button>
      </form>
    </section>
  </main>;
}
