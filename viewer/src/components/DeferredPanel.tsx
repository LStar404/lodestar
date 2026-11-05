import { Component, Suspense, type ReactNode } from "react";

/** 延迟面板加载失败时保留阅读区与草稿，用户可关闭提示继续阅读。 */
export default class DeferredPanel extends Component<{ children: ReactNode; onClose: () => void }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (this.state.failed) return <div className="modal-mask">
      <div className="modal" role="alert">
        <h2>暂时无法打开面板</h2>
        <p>请检查连接，保存笔记后刷新页面重试。</p>
        <button onClick={this.props.onClose}>关闭提示</button>
      </div>
    </div>;
    return <Suspense fallback={<div className="toast" role="status">正在打开面板…</div>}>
      {this.props.children}
    </Suspense>;
  }
}
