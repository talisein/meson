function fhelper(x) result(r) bind(C, name="fhelper")
  use, intrinsic :: iso_c_binding, only: c_int
  integer(c_int), value :: x
  integer(c_int) :: r
  r = x + 1
end function fhelper
