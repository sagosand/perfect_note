o.bind("SUPER + N", "Perfect Note", "/usr/bin/python3 ~/Work/perfect_note/perfect_note.py")

o.window("^io\\.github\\.sagosand\\.PerfectNote$", {
  float = true,
  center = true,
  pin = true,
  size = { 638, 845 },
  rounding = 14,
  border_size = 1,
  opacity = "1 1",
})
