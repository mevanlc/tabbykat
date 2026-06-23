#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
kitty_config="${HOME}/.config/kitty"

mkdir -p -- "${kitty_config}"

link_file() {
  local name="$1"
  local source_path="${repo_root}/${name}"
  local target_path="${kitty_config}/${name}"

  if [[ ! -e "${source_path}" ]]; then
    printf 'missing source: %s\n' "${source_path}" >&2
    return 1
  fi

  if [[ -L "${target_path}" ]]; then
    local current_target
    current_target="$(readlink -- "${target_path}")"
    if [[ "${current_target}" == "${source_path}" ]]; then
      printf 'ok: %s -> %s\n' "${target_path}" "${source_path}"
      return 0
    fi

    ln -sfn -- "${source_path}" "${target_path}"
    printf 'updated: %s -> %s\n' "${target_path}" "${source_path}"
    return 0
  fi

  if [[ -e "${target_path}" ]]; then
    printf 'refusing to replace non-symlink: %s\n' "${target_path}" >&2
    return 1
  fi

  ln -s -- "${source_path}" "${target_path}"
  printf 'created: %s -> %s\n' "${target_path}" "${source_path}"
}

link_file tab_bar.py
link_file tab_bar.toml
