module fmod
  use, intrinsic :: iso_c_binding, only: c_int
  implicit none
contains
  function add_one(x) result(r)
    integer(c_int), value :: x
    integer(c_int) :: r
    r = x + 1
  end function add_one
end module fmod
