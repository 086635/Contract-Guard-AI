import { create } from "zustand";

/** 合同级状态库（Zustand）：AST 节点树与分层 Memory（§10）。 */
interface ContractState {
  contractId: string | null;
  astTopology: unknown[]; // ClauseNode[]（生成类型后替换为具体类型）
  setContract: (id: string) => void;
  setAst: (nodes: unknown[]) => void;
  clearContract: () => void;
}

export const useContractStore = create<ContractState>((set) => ({
  contractId: null,
  astTopology: [],
  setContract: (id) => set((state) => ({ contractId: id, astTopology: state.contractId === id ? state.astTopology : [] })),
  setAst: (nodes) => set({ astTopology: nodes }),
  clearContract: () => set({ contractId: null, astTopology: [] }),
}));
