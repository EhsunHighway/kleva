#ifndef KLEVA_DOCKER_SMOKE_LUCKY_H
#define KLEVA_DOCKER_SMOKE_LUCKY_H

/*@
  requires x >= 0;
  requires x <= 10;
  assigns \nothing;
  ensures \result == 7;
*/
int lucky(int x);

#endif
