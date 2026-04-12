return {
  {
    "nvim-neo-tree/neo-tree.nvim",
    opts = {
      filesystem = {
        filtered_items = {
          hide_dotfiles = false,
          hide_gitignored = false,
        },
      },
      window = {
        position = "left",
        width = 30,
        mapping_options = {
          noremap = true,
          nowait = true,
        },
      },
    },
    config = function(_, opts)
      require("neo-tree").setup(opts)
      -- Neotree 打开逻辑已移到 autocmds.lua，这里不再重复
    end,
  },
  {
    "akinsho/bufferline.nvim",
    event = "VeryLazy",
    opts = {
      options = {
        mode = "buffers", -- set to "tabs" to only show tabpages instead
        numbers = "ordinal",
        close_command = function(bufnr) vim.api.nvim_buf_delete(bufnr, { force = true }) end,
        right_mouse_command = function(bufnr) vim.api.nvim_buf_delete(bufnr, { force = true }) end,
        left_mouse_command = "buffer",
        middle_mouse_command = nil,
        indicator = {
          style = "icon",
          icon = "▎",
        },
        buffer_close_icon = "󰅖",
        modified_icon = "●",
        close_icon = "",
        left_trunc_marker = "",
        right_trunc_marker = "",
        max_name_length = 18,
        max_prefix_length = 15,
        tab_size = 18,
        truncate_names = true,
        color_icons = true,
        show_buffer_icons = true,
        show_buffer_close_icons = true,
        show_close_icon = true,
        show_tab_indicators = true,
        persist_buffer_sort = true,
        separator_style = "thin",
        enforce_regular_tabs = false,
        always_show_bufferline = true,
        hover = {
          enabled = true,
          delay = 200,
          reveal = {'close'}
        },
        sort_by = 'insert_after_current',
      },
    },
    config = function(_, opts)
      require("bufferline").setup(opts)
    end,
  },
}