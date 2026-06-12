package com.amazon.ateskywalkerquery.activity;

import com.amazon.ateskywalkerquery.Beer;
import com.amazon.ateskywalkerquery.BeerList;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.util.UUID;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.fail;

/**
 *
 * A collection of unit tests for the BeerActivity class. This set
 * tests each method of the BeerActivity class by using the Coral mock client
 * which will verify that the Coral layer, such as our interceptor, is being
 * called as if it were running in the server context.
 *
 */
class BeerActivitiesTest {

    private BeerActivity activity;

    @BeforeEach
    public void setup() {
        activity = new BeerActivity();
    }

    /**
     * Test for getAllBeers()
     */
    @Test
    public void testGetAllBeers() {
        // Create lots of beers, verify that all are accounted for
        Beer[] beers = createBeers(100);
        BeerList beerList = activity.getAllBeers();
        assertEquals(beers.length, beerList.getBeers().size());
        for (Beer beer : beers) {
            assertBeerListContains(beerList, beer);
        }
    }

    /**
     * Test for createBeer()
     */
    @Test
    public void testCreateBeer() {
        // Create the beer via the activity class
        Beer beerToCreate = new Beer();
        beerToCreate.setName(getRandomString());
        beerToCreate.setDescription(getRandomString());
        beerToCreate.setBeerCompanyName(getRandomString());
        beerToCreate.setBeerTypeName(getRandomString());

        Beer createdBeer = activity.createBeer(beerToCreate);

        // Validate what we sent in is still there
        // We cannot use the equals method on the object since the beerId is
        // assigned by the call
        assertEquals(beerToCreate.getName(), createdBeer.getName());
        assertEquals(beerToCreate.getDescription(), createdBeer.getDescription());
        assertEquals(beerToCreate.getBeerCompanyName(), createdBeer.getBeerCompanyName());
        assertEquals(beerToCreate.getBeerTypeName(), createdBeer.getBeerTypeName());

        // Validate we got back an id
        assertNotNull(createdBeer.getBeerId());

        BeerList beerList = activity.getAllBeers();
        assertBeerListContains(beerList, createdBeer);
    }

    /*
     *
     * Data validation methods
     *
     */

    /**
     * Checks the BeerList contains the given Beer.
     *
     * @param beerList The beer list to check
     * @param beer The target beer
     */
    private void assertBeerListContains(BeerList beerList, Beer beer) {
        for (Beer listBeer : beerList.getBeers()) {
            if (listBeer.equals(beer)) {
                return;
            }
        }
        fail("Expected beer was not found.");
    }

    /*
     *
     * DB population methods
     *
     */

    /**
     * Creates a new set of beer models
     *
     * @param amount The number of beers to create
     * @return The array of beers it created
     */
    private Beer[] createBeers(int amount) {
        Beer[] beers = new Beer[amount];

        for (int i = 0; i < amount; i++) {
            Beer beer = new Beer();
            beer.setName(i + getRandomString());
            beer.setDescription(getRandomString());
            beer.setBeerCompanyName(getRandomString());
            beer.setBeerTypeName(getRandomString());
            beers[i] = activity.createBeer(beer);
        }

        return beers;
    }

    /**
     * Returns a random string
     */
    protected String getRandomString() {
        return UUID.randomUUID().toString();
    }

    /**
     * Clear out beers from database's store
     */
    @BeforeEach
    public void clearBeers() {
        BeerActivity.clearBeers();
    }
}
